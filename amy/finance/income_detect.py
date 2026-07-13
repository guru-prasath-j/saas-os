"""Detect likely recurring income sources (salary, retainer, rent received)
from transaction history.

Same pre-filter-then-one-call pattern as subscription_detect.py, mirrored
onto the credit side: same merchant, consistent amount, ~monthly cadence.
Bank interest credits are excluded outright (auto-generated, grows with
balance so it isn't a stable "amount", and isn't something a user manages
as a named income source) rather than relying on the amount-tolerance
filter to catch them.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime

_MIN_OCCURRENCES = 2
_AMOUNT_TOLERANCE = 0.15   # ±15% counts as "same" recurring amount
_MIN_GAP_DAYS = 5          # weekly retainers are a real pattern, not just monthly
_MAX_GAP_DAYS = 40

_EXCLUDE_KEYWORDS = ("interest credit", "interest cr", "refund", "reversal", "cashback")

_TYPE_KEYWORDS = (
    ("salary", ("salary", "payroll", "sal cr")),
    ("freelance", ("freelance", "project", "contract", "consulting", "gig")),
    ("rental", ("rent received", "rental")),
    ("business", ("sales", "invoice", "settlement", "business")),
)

_SYSTEM = (
    "You review candidate recurring credits from a bank statement and "
    "decide which are real income sources (salary, freelance retainer, "
    "rent received, recurring business payment) versus coincidental repeat "
    "credits (e.g. a friend paying back the same amount twice) or bank-"
    "generated credits (interest, refunds, cashback — never income). "
    "For each candidate return JSON: "
    '[{"idx":0,"is_income":true,"name":"Salary – Acme Tech Solutions",'
    '"income_type":"salary","recurrence":"monthly","confidence":0.9}, ...]. '
    "income_type is one of: salary, freelance, rental, business, other. "
    "recurrence is one of: weekly, monthly, annual. name should be a clean "
    "human-readable label, not the raw bank narration. Return ONLY the "
    "JSON array."
)


def _parse_date(s: str):
    try:
        return datetime.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _guess_type(merchant: str) -> str:
    low = merchant.lower()
    for income_type, keywords in _TYPE_KEYWORDS:
        if any(k in low for k in keywords):
            return income_type
    return "other"


def _guess_recurrence(avg_gap: float) -> str:
    if avg_gap <= 10:
        return "weekly"
    if avg_gap >= 300:
        return "annual"
    return "monthly"


def _find_candidates(transactions: list[dict]) -> list[dict]:
    # Bucket by (account_id, merchant) — see subscription_detect.py's
    # _find_candidates for why: merging the same merchant name across two
    # different accounts interleaves their credit dates/amounts and
    # corrupts the cadence/amount checks below.
    by_merchant: dict[tuple, list[dict]] = defaultdict(list)
    for t in transactions:
        if t.get("amount", 0) <= 0:
            continue  # only incoming credits can be an income source
        merchant = (t.get("merchant") or "").strip()
        if not merchant:
            continue
        if any(k in merchant.lower() for k in _EXCLUDE_KEYWORDS):
            continue
        by_merchant[(t.get("account_id"), merchant.lower())].append(t)

    candidates = []
    for txns in by_merchant.values():
        if len(txns) < _MIN_OCCURRENCES:
            continue
        txns = sorted(txns, key=lambda t: t.get("date", ""))
        amounts = [t["amount"] for t in txns]
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

        merchant = txns[-1]["merchant"]
        candidates.append({
            "merchant": merchant,
            "amount": round(avg_amount, 2),
            "occurrences": len(txns),
            "last_date": txns[-1]["date"],
            "income_type": _guess_type(merchant),
            "recurrence": _guess_recurrence(avg_gap),
        })
    return candidates


def _already_tracked(merchant: str, existing_names: list[str]) -> bool:
    m = re.sub(r"[^a-z0-9]", "", merchant.lower())
    for name in existing_names:
        n = re.sub(r"[^a-z0-9]", "", name.lower())
        if n and (n in m or m in n):
            return True
    return False


def detect_income(engine, llm) -> list[dict]:
    """Return suggested income sources not already tracked, for review in
    the UI. Re-runs fresh on every call (no persistence), same stance as
    detect_subscriptions."""
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=200)).isoformat()
    transactions = engine.list_transactions(limit=2000, since=since)
    candidates = _find_candidates(transactions)
    if not candidates:
        return []

    existing_names = [s["name"] for s in engine.list_income_sources()]
    candidates = [c for c in candidates
                  if not _already_tracked(c["merchant"], existing_names)]
    if not candidates:
        return []

    for c in candidates:
        c["name"] = c["merchant"]
        c["confidence"] = 0.6  # rule-only default if LLM is unavailable/fails

    result = candidates
    if llm is not None:
        lines = "\n".join(
            f'{i}. {c["merchant"]} | ₹{c["amount"]:.0f} x{c["occurrences"]} '
            f'| last {c["last_date"]} | guessed {c["income_type"]}/{c["recurrence"]}'
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
                    if not item.get("is_income", True):
                        continue
                    c = candidates[idx]
                    c["name"] = item.get("name") or c["merchant"]
                    c["income_type"] = item.get("income_type") or c["income_type"]
                    c["recurrence"] = item.get("recurrence") or c["recurrence"]
                    c["confidence"] = item.get("confidence", 0.6)
                    kept.append(c)
                result = kept
        except Exception:
            pass  # degrade to rule-only candidates

    return result
