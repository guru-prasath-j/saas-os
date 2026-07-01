"""Detect likely recurring subscriptions from transaction history.

Cheap rule-based grouping narrows transactions down to a handful of
candidates (same merchant, consistent amount, ~monthly cadence) before a
single batch LLM call confirms/labels them — same pre-filter-then-one-call
pattern as Gmail enrichment, so this stays cheap even on large histories.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime

_MIN_OCCURRENCES = 2
_AMOUNT_TOLERANCE = 0.15   # ±15% counts as "same" recurring amount
_MIN_GAP_DAYS = 20
_MAX_GAP_DAYS = 40

_SYSTEM = (
    "You review candidate recurring charges from a bank statement and decide "
    "which ones are real subscriptions (Netflix, gym, rent, SaaS, insurance "
    "premium, etc.) versus coincidental repeat purchases (e.g. two unrelated "
    "grocery runs of a similar amount). For each candidate return JSON: "
    '[{"idx":0,"is_subscription":true,"name":"Netflix","billing_cycle":"monthly",'
    '"confidence":0.9}, ...]. billing_cycle is "monthly" or "yearly" based on the '
    "gap between charges. name should be a clean human-readable label, not the raw "
    "bank narration. Return ONLY the JSON array."
)


def _parse_date(s: str):
    try:
        return datetime.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _find_candidates(transactions: list[dict]) -> list[dict]:
    by_merchant: dict[str, list[dict]] = defaultdict(list)
    for t in transactions:
        if t.get("amount", 0) >= 0:
            continue  # only outgoing spend can be a subscription
        merchant = (t.get("merchant") or "").strip()
        if not merchant:
            continue
        by_merchant[merchant.lower()].append(t)

    candidates = []
    for txns in by_merchant.values():
        if len(txns) < _MIN_OCCURRENCES:
            continue
        txns = sorted(txns, key=lambda t: t.get("date", ""))
        amounts = [abs(t["amount"]) for t in txns]
        avg_amount = sum(amounts) / len(amounts)
        if avg_amount == 0 or any(
            abs(a - avg_amount) / avg_amount > _AMOUNT_TOLERANCE for a in amounts
        ):
            continue

        dates = [d for d in (_parse_date(t.get("date")) for t in txns) if d]
        if len(dates) < _MIN_OCCURRENCES:
            continue
        gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        avg_gap = sum(gaps) / len(gaps)
        if not (_MIN_GAP_DAYS <= avg_gap <= _MAX_GAP_DAYS):
            continue

        candidates.append({
            "merchant": txns[-1]["merchant"],
            "amount": round(avg_amount, 2),
            "occurrences": len(txns),
            "last_date": txns[-1]["date"],
            "category": txns[-1].get("category", ""),
        })
    return candidates


def _already_tracked(merchant: str, existing_names: list[str]) -> bool:
    m = re.sub(r"[^a-z0-9]", "", merchant.lower())
    for name in existing_names:
        n = re.sub(r"[^a-z0-9]", "", name.lower())
        if n and (n in m or m in n):
            return True
    return False


def _estimate_next_due(last_date: str, billing_cycle: str) -> str | None:
    d = _parse_date(last_date)
    if not d:
        return None
    from datetime import timedelta
    d = d + (timedelta(days=365) if billing_cycle == "yearly" else timedelta(days=30))
    return d.date().isoformat()


def detect_subscriptions(engine, llm) -> list[dict]:
    """Return suggested subscriptions not already tracked, for review in the UI."""
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=200)).isoformat()
    transactions = engine.list_transactions(limit=2000, since=since)
    candidates = _find_candidates(transactions)
    if not candidates:
        return []

    existing_names = [s["name"] for s in engine.list_subscriptions(status=None)]
    candidates = [c for c in candidates
                  if not _already_tracked(c["merchant"], existing_names)]
    if not candidates:
        return []

    for c in candidates:
        c["name"] = c["merchant"]
        c["billing_cycle"] = "monthly"
        c["confidence"] = 0.6  # rule-only default if LLM is unavailable/fails

    result = candidates
    if llm is not None:
        lines = "\n".join(
            f'{i}. {c["merchant"]} | ₹{c["amount"]:.0f} x{c["occurrences"]} '
            f'| last {c["last_date"]} | category {c["category"] or "?"}'
            for i, c in enumerate(candidates)
        )
        try:
            raw_resp, _ = llm.generate(_SYSTEM, f"Candidates:\n{lines}", sensitive=False)
            raw_resp = re.sub(r"```(?:json)?", "", raw_resp).strip()
            start, end = raw_resp.find("["), raw_resp.rfind("]")
            if start != -1 and end != -1:
                verdicts = json.loads(raw_resp[start:end + 1])
                kept = []
                for item in verdicts:
                    idx = item.get("idx")
                    if idx is None or not (0 <= idx < len(candidates)):
                        continue
                    if not item.get("is_subscription", True):
                        continue
                    c = candidates[idx]
                    c["name"] = item.get("name") or c["merchant"]
                    c["billing_cycle"] = item.get("billing_cycle", "monthly")
                    c["confidence"] = item.get("confidence", 0.6)
                    kept.append(c)
                result = kept
        except Exception:
            pass  # degrade to rule-only candidates

    for c in result:
        c["next_due"] = _estimate_next_due(c["last_date"], c["billing_cycle"])
    return result
