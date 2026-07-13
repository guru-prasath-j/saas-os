"""Detect likely recurring investments (SIPs, broker debits) from transaction
history.

Same pre-filter-then-one-call pattern as subscription_detect.py: cheap
rule-based grouping (same merchant, consistent amount, ~monthly cadence)
narrows candidates down before a single batch LLM call classifies the
investment type and cleans the name. Debit transactions only, and either
already categorized "Investment" by the rule-based categorizer or matching
the same investment keyword set it uses — so this only ever proposes what
a human would already recognize as an investment debit, never a guess.
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

# Same keyword set amy/finance/categorizer.py uses to tag a transaction
# "Investment" — reused here (not imported: categorizer's set is private to
# its own rule table shape) so a debit still qualifies even if the user's
# categorizer run predates this keyword landing, or a rule was overridden.
_INVESTMENT_KEYWORDS = (
    "sip", "mutual fund", "ppf", "nps", "fd-", "fixed deposit",
    "hdfc securities", "zerodha", "groww", "kuvera", "paytm money",
    "scripbox", "investment", "demat", "ipo-", "dividend", "smallcase",
    "angel broking", "5paisa", "upstox",
)

_TYPE_KEYWORDS = (
    ("mutual_fund", ("sip", "mutual fund", "index fund", "bluechip", "elss")),
    ("stock", ("zerodha", "upstox", "angel broking", "5paisa", "demat", "broking", "stock")),
    ("gold", ("gold", "sgb", "digital gold")),
    ("ppf", ("ppf",)),
    ("nps", ("nps",)),
    ("fd", ("fd-", "fixed deposit")),
    ("crypto", ("crypto", "bitcoin", "coindcx", "wazirx", "binance")),
)

_SYSTEM = (
    "You review candidate recurring investment debits from a bank statement "
    "and decide which are real ongoing investments (SIP, recurring broker "
    "transfer, RD/PPF/NPS contribution) versus coincidental repeat debits. "
    "For each candidate return JSON: "
    '[{"idx":0,"is_investment":true,"name":"SBI Bluechip SIP",'
    '"investment_type":"mutual_fund","confidence":0.9}, ...]. '
    "investment_type is one of: mutual_fund, stock, fd, gold, ppf, nps, "
    "crypto, other. name should be a clean human-readable label, not the "
    "raw bank narration. Return ONLY the JSON array."
)


def _parse_date(s: str):
    try:
        return datetime.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _looks_like_investment(t: dict) -> bool:
    if (t.get("category") or "").strip().lower() == "investment":
        return True
    merchant = (t.get("merchant") or "").lower()
    return any(k in merchant for k in _INVESTMENT_KEYWORDS)


def _guess_type(merchant: str) -> str:
    low = merchant.lower()
    for inv_type, keywords in _TYPE_KEYWORDS:
        if any(k in low for k in keywords):
            return inv_type
    return "other"


def _find_candidates(transactions: list[dict]) -> list[dict]:
    # Bucket by (account_id, merchant) — see subscription_detect.py's
    # _find_candidates for why: the same merchant name recurring across two
    # different accounts (household's two cards, etc.) must not be merged
    # before the cadence/amount checks, or a real SIP in one account gets
    # dropped because it's averaged against an unrelated debit of a
    # different amount in the other account.
    by_merchant: dict[tuple, list[dict]] = defaultdict(list)
    for t in transactions:
        if t.get("amount", 0) >= 0:
            continue  # only outgoing debits can be a contribution
        if not _looks_like_investment(t):
            continue
        merchant = (t.get("merchant") or "").strip()
        if not merchant:
            continue
        by_merchant[(t.get("account_id"), merchant.lower())].append(t)

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

        merchant = txns[-1]["merchant"]
        candidates.append({
            "merchant": merchant,
            "amount": round(avg_amount, 2),
            # cost basis = every contribution actually seen so far, not just
            # one installment — this is what's genuinely been put in.
            "cost_basis": round(sum(amounts), 2),
            "occurrences": len(txns),
            "last_date": txns[-1]["date"],
            "investment_type": _guess_type(merchant),
        })
    return candidates


def _already_tracked(merchant: str, existing_names: list[str]) -> bool:
    m = re.sub(r"[^a-z0-9]", "", merchant.lower())
    for name in existing_names:
        n = re.sub(r"[^a-z0-9]", "", name.lower())
        if n and (n in m or m in n):
            return True
    return False


def detect_investments(engine, llm) -> list[dict]:
    """Return suggested investments not already tracked, for review in the UI.

    current_value defaults to cost_basis (sum of contributions seen) — same
    honesty stance as the manual Investments tab: this app has no live
    market-price feed, so it never guesses a current value it can't back up.
    The user can edit it after accepting, once they check the real NAV.
    """
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=200)).isoformat()
    transactions = engine.list_transactions(limit=2000, since=since)
    candidates = _find_candidates(transactions)
    if not candidates:
        return []

    existing_names = [i["name"] for i in engine.list_investments()]
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
            f'| total so far ₹{c["cost_basis"]:.0f} | last {c["last_date"]}'
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
                    if not item.get("is_investment", True):
                        continue
                    c = candidates[idx]
                    c["name"] = item.get("name") or c["merchant"]
                    c["investment_type"] = item.get("investment_type") or c["investment_type"]
                    c["confidence"] = item.get("confidence", 0.6)
                    kept.append(c)
                result = kept
        except Exception:
            pass  # degrade to rule-only candidates

    for c in result:
        c["current_value"] = c["cost_basis"]
    return result
