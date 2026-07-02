"""Compliance pipeline: reuses ledger entries from the Ledger tab automatically
(no second upload). For each ledger entry without a suggestion yet —
  1. Route by sensitivity — GSTIN/PAN-bearing entries go through the local
     Ollama-only path (sensitivity.is_sensitive + LLMRouter.pick(sensitive=True)
     in amy/llm.py); everything else uses the normal cascade.
  2. Look up current rates from rate_table (rates.lookup) — never let the LLM
     invent a GST rate or depreciation block from training-data recall.
  3. Classify & calculate via one batch LLM call per sensitivity group,
     mirroring amy/finance/subscription_detect.py's candidate-filter ->
     one-call -> structured-JSON shape.
  4. Every suggestion carries reasoning, a citation back to the source entry,
     and a persistent "confirm with your CA" framing. Nothing here files or
     submits anything to any tax authority — no such capability exists in
     this module by construction.
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from . import rates as _rates
from .sensitivity import is_sensitive

if TYPE_CHECKING:
    from ..engine import FinanceEngine

CA_DISCLAIMER = "Confirm with your CA before acting on this."

_SYSTEM = """You are a compliance assistant for a small Indian business. For \
each candidate ledger entry, decide whether it implies a GST or depreciation \
consideration, using ONLY the rate table provided (never invent a rate from \
memory). Return ONLY a JSON array, no markdown fences, no explanation. Each \
element must have exactly these keys:
  "idx"             : the index number exactly as given
  "suggestion_type" : one of "gst", "depreciation", "threshold", "none"
  "reasoning"       : one or two sentences explaining the suggestion, citing
                       the specific rate/block used from the rate table
  "rate_key"        : the "key" of the rate_table row used, or null
If an entry has no compliance implication, use suggestion_type "none" — it
will be skipped. Always ground reasoning in the given rate table; never state
a percentage or block that isn't in it."""


def _build_prompt(candidates: list[dict], rate_rows: list[dict]) -> str:
    rate_lines = "\n".join(
        f'- {r["rate_type"]}.{r["key"]}: {json.dumps(r["value"])} ({r["source_note"]})'
        for r in rate_rows
    )
    entry_lines = "\n".join(
        f'{c["idx"]}. {c["date"]} | ₹{abs(c["amount"]):.0f} '
        f'({"in" if c["amount"] > 0 else "out"}) | {c["description"]}'
        for c in candidates
    )
    return f"Rate table:\n{rate_lines}\n\nCandidate entries:\n{entry_lines}"


def _call_llm(candidates: list[dict], rate_rows: list[dict], llm, sensitive: bool) -> list[dict]:
    if not candidates or llm is None:
        return []
    try:
        prompt = _build_prompt(candidates, rate_rows)
        raw_resp, _ = llm.generate(_SYSTEM, prompt, sensitive=sensitive)
        raw_resp = re.sub(r"```(?:json)?", "", raw_resp).strip()
        start, end = raw_resp.find("["), raw_resp.rfind("]")
        if start == -1 or end == -1:
            return []
        return json.loads(raw_resp[start:end + 1])
    except Exception:
        return []


def generate_suggestions(fe: "FinanceEngine", entity: dict, llm) -> list[dict]:
    """Return a list of {ledger_entry_id, suggestion_type, reasoning,
    rate_used, citation, routed_sensitive} dicts — does not write to the DB;
    the router persists each one alongside a fresh EventStore event id."""
    pending = fe.ledger_entries_without_suggestions(entity["id"])
    if not pending:
        return []

    rate_rows = _rates.lookup(fe)
    rate_by_key = {r["key"]: r for r in rate_rows}

    sensitive_candidates, normal_candidates = [], []
    for idx, e in enumerate(pending):
        c = {"idx": idx, "date": e["date"], "amount": e["amount"],
             "description": e["description"], "_entry": e}
        # Scope this to the ENTRY's own text, not the entity's PAN/GSTIN —
        # those are checked separately at ingestion time (accountant.py scans
        # the whole source document). Passing entity.pan/gstin in here would
        # mark every entry of any GST-registered business as sensitive
        # permanently, forcing 100% of entries through slow one-at-a-time
        # local calls instead of the fast batched cloud path (observed: this
        # made "Run Compliance Pass" hang for minutes on a normal ledger).
        if is_sensitive(e["description"]):
            sensitive_candidates.append(c)
        else:
            normal_candidates.append(c)

    def _apply(verdicts: list[dict], group: list[dict], sensitive_flag: bool) -> list[dict]:
        by_idx = {c["idx"]: c for c in group}
        out = []
        for v in verdicts:
            c = by_idx.get(v.get("idx"))
            if c is None or v.get("suggestion_type") in (None, "none"):
                continue
            entry = c["_entry"]
            rate_row = rate_by_key.get(v.get("rate_key"))
            out.append({
                "ledger_entry_id": entry["id"],
                "suggestion_type": v["suggestion_type"],
                "reasoning": v.get("reasoning", ""),
                "rate_used": json.dumps(rate_row) if rate_row else None,
                "citation": f"ledger entry {entry['id']} "
                            f"({entry['date']}, {entry['description'] or 'no description'})"
                            + (f", source: {entry['source_document']}" if entry.get("source_document") else ""),
                "ca_disclaimer": CA_DISCLAIMER,
                "routed_sensitive": sensitive_flag,
            })
        return out

    results: list[dict] = []
    # Local (Ollama) models are noticeably less reliable at tracking which
    # "idx" a piece of reasoning belongs to inside a multi-item batch prompt
    # — observed in testing: reasoning about one entry got attached to a
    # different entry's citation. The sensitive group is forced through that
    # less reliable path, so call it one entry at a time (idx is trivially
    # unambiguous with a single candidate) instead of batching. The normal
    # group keeps the cheaper single-batch-call shape since the cloud model
    # tracks multi-item idx reliably.
    for c in sensitive_candidates:
        verdicts = _call_llm([c], rate_rows, llm, True)
        results.extend(_apply(verdicts, [c], True))
    if normal_candidates:
        verdicts = _call_llm(normal_candidates, rate_rows, llm, False)
        results.extend(_apply(verdicts, normal_candidates, False))
    return results
