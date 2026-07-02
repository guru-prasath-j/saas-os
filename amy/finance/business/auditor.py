"""Auditor: a read-only fidelity check of already-posted ledger entries
against the source document they came from — NOT a second-opinion review
(there is only one writer, the Accountant). Structured like
amy/finance/custodial.py's run_validation(): rule-based, no LLM call, never
mutates amounts/descriptions, only ever updates audit_status on the rows it
checks.

Only invoked when business_entities.tracking_closeness == 'close' — the
router checks that gate before calling in, so this module stays a plain
function callable in tests without the gate logic embedded.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..engine import FinanceEngine

_AMOUNT_RE = re.compile(r"\d[\d,]*\.?\d*")
_TOLERANCE = 0.02  # 2% — allows for rounding in hand-kept logs


def _numbers_in_text(text: str) -> list[float]:
    out = []
    for m in _AMOUNT_RE.findall(text):
        try:
            v = float(m.replace(",", ""))
            if v > 0:
                out.append(v)
        except ValueError:
            continue
    return out


def run_audit(fe: "FinanceEngine", entity_id: str, source_text: str) -> dict:
    """Compare all posted ledger_entries for this entity against source_text —
    flags entries whose amount doesn't appear in the source, and flags a
    total mismatch if the sum of ledger entries doesn't match the largest
    number mentioned in the source (a common statement/log "total" line)."""
    entries = fe.list_ledger_entries(entity_id)
    issues: list[dict] = []
    source_numbers = _numbers_in_text(source_text)

    for e in entries:
        amt = abs(e["amount"])
        found = any(abs(amt - n) / n <= _TOLERANCE for n in source_numbers if n)
        if found:
            fe.set_ledger_audit_status(e["id"], "ok")
        else:
            fe.set_ledger_audit_status(e["id"], "flagged")
            issues.append({
                "check": "amount_not_in_source",
                "ledger_entry_id": e["id"],
                "amount": e["amount"],
                "description": e["description"],
            })

    if entries and source_numbers:
        ledger_total = sum(abs(e["amount"]) for e in entries)
        # A source doc may state an explicit grand total (usually the largest
        # number present) OR simply list line items with no total line at all
        # — an informal log rarely has one. Accept either as a match so a
        # normal, total-line-free log doesn't spuriously fail this check.
        candidate_totals = {max(source_numbers), round(sum(source_numbers), 2)}
        if not any(t and abs(ledger_total - t) / t <= _TOLERANCE for t in candidate_totals):
            issues.append({
                "check": "total_mismatch",
                "ledger_total": round(ledger_total, 2),
                "source_document_total_guess": max(candidate_totals),
            })

    return {
        "issues": issues,
        "entries_checked": len(entries),
        "checked_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
