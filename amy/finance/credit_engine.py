"""Amy Credit Score Module (Phase 3) — an illustrative INTERNAL score, not a
real credit bureau product. Phase 3 of the same "Banking Risk Intelligence"
series as fraud_engine.py (Phase 1) and aml_engine.py (Phase 2), which this
module consumes as inputs rather than duplicating their detection logic.

Every generated explanation/detail string in this module must call the
score "Amy Score — an internal signal, not a credit bureau score." Never
FICO/CIBIL/an equivalent, never presented as reconciled with a real bureau
— there is no bureau integration anywhere in this codebase (bureau_score
below is always available:false). All weights/curves are illustrative
placeholders, commented `# illustrative weight/curve, not sourced from a
real scoring model`.

Two honesty notes worth knowing before touching this file (also in
CLAUDE.md's quirks):

1. `payment_history` is a PROXY, not real bill/loan payment history. This
   schema has no bill/loan/credit-card payment-obligation tracking —
   `amy/commitments/engine.py` tracks return windows/warranties/renewals/
   documents (an "expired" row means a return window lapsed, not that a
   bill went unpaid), and `subscriptions.status` is free-text with no
   "missed payment" semantic anywhere in the codebase. This factor is
   built from commitment completion rate + subscription non-active ratio
   instead, and its own `detail` text says so.

2. There is no account-balance column anywhere in this schema (`accounts`
   has no `balance` field; `FinanceEngine.balance_estimate()` is a
   monthly income-minus-spend delta, not a running balance). So
   `overdrafts` is always `available:false`, and "savings" is folded into
   `investment_profile` instead of being a separate factor that would
   just re-read the same `investments` table under a different name.
"""
from __future__ import annotations

import datetime as _dt
from collections import defaultdict

# ---------------------------------------------------------------------------
# Illustrative weights — sum to 100 when every conditionally-available
# factor is actually available; overdrafts/bureau_score always weight 0.
# None of these are sourced from a real scoring model.
# ---------------------------------------------------------------------------

WEIGHTS = {
    "payment_history": 20,      # illustrative weight, not sourced from a real scoring model
    "income_stability": 15,     # illustrative weight, not sourced from a real scoring model
    "cashflow_trend": 15,       # illustrative weight, not sourced from a real scoring model
    "debt": 12,                 # illustrative weight, not sourced from a real scoring model
    "fraud_history": 10,        # illustrative weight, not sourced from a real scoring model
    "aml_alerts": 10,           # illustrative weight, not sourced from a real scoring model
    "investment_profile": 10,   # illustrative weight, not sourced from a real scoring model
    "business_stability": 8,    # illustrative weight, not sourced from a real scoring model
    "overdrafts": 0,            # always available:false — see module docstring
    "bureau_score": 0,          # always available:false — no bureau integration exists
}

SCORE_FLOOR = 300
SCORE_CEIL = 900

_LABELS = {
    "payment_history": "payment/commitment reliability",
    "income_stability": "income stability",
    "cashflow_trend": "cashflow trend",
    "debt": "debt burden",
    "fraud_history": "fraud history",
    "aml_alerts": "AML alerts",
    "investment_profile": "investment profile",
    "business_stability": "business stability",
    "overdrafts": "overdrafts",
    "bureau_score": "bureau score",
}

_SUGGESTION_TEMPLATES = {
    "payment_history": "Keep tracked commitments (return windows/warranties/renewals) "
                       "from expiring — this system's closest available proxy for "
                       "payment reliability, since it has no real bill/loan tracking.",
    "income_stability": "Add or confirm a recurring monthly income source for a more "
                        "stable income-stability reading.",
    "cashflow_trend": "Aim for more months where income exceeds spend.",
    "debt": "Reduce EMI/Loan-categorized debt relative to your effective income.",
    "investment_profile": "Diversify or grow your tracked investments.",
    "fraud_history": "Review any flagged transactions in the Fraud tab.",
    "aml_alerts": "Resolve or close any open AML cases.",
    "business_stability": "Get flagged ledger entries resolved by the business Auditor.",
}


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _available(value: float, detail: str) -> dict:
    return {"available": True, "value": round(max(0.0, min(100.0, value)), 1), "detail": detail}


def _unavailable(reason: str) -> dict:
    return {"available": False, "reason": reason}


# ---------------------------------------------------------------------------
# Factors
# ---------------------------------------------------------------------------

def _factor_payment_history(fe) -> dict:
    from ..commitments import CommitmentEngine

    commitments = CommitmentEngine(fe).list(status=None)
    done = sum(1 for c in commitments if c["status"] in ("done", "dismissed"))
    expired = sum(1 for c in commitments if c["status"] == "expired")
    subs = fe.list_subscriptions(status=None)
    active_subs = sum(1 for s in subs if (s.get("status") or "").lower() == "active")

    value = 70.0   # neutral baseline — illustrative curve, not sourced from a real scoring model
    if commitments:
        value = max(0.0, 100.0 - expired * 15.0)   # illustrative curve
    if subs:
        sub_component = 100.0 * active_subs / len(subs)
        value = (value + sub_component) / 2 if commitments else sub_component

    detail = (f"PROXY signal, not verified bill/loan payment history (this system "
             f"tracks no such data): {len(commitments)} tracked commitment(s) "
             f"({done} done/dismissed, {expired} expired) and {len(subs)} "
             f"subscription(s) ({active_subs} active).")
    return _available(value, detail)


def _factor_income_stability(fe) -> dict:
    sources = fe.list_income_sources()
    monthly_income = fe.effective_monthly_income()
    if not sources and monthly_income <= 0:
        return _unavailable("no declared income sources and no income transactions observed")
    value = 40.0   # illustrative curve, not sourced from a real scoring model
    if sources:
        value += 20.0
        if any((s.get("recurrence") or "").lower() == "monthly" for s in sources):
            value += 20.0
    if monthly_income > 0:
        value += 20.0
    detail = (f"{len(sources)} declared income source(s); observed effective "
             f"monthly income ~{monthly_income:,.0f}.")
    return _available(value, detail)


def _factor_cashflow_trend(fe) -> dict:
    txns = fe.list_transactions(limit=5000)
    by_month: dict[str, float] = defaultdict(float)
    for t in txns:
        month = (t.get("date") or "")[:7]
        if month:
            by_month[month] += t.get("amount") or 0
    months = sorted(by_month.keys())
    if len(months) < 2:
        return _unavailable("fewer than 2 distinct months of transaction history")
    values = [by_month[m] for m in months]
    half = len(values) // 2
    older_avg = sum(values[:half]) / half
    recent_avg = sum(values[half:]) / (len(values) - half)
    positive_share = sum(1 for v in values if v > 0) / len(values)

    value = 50.0   # illustrative curve, not sourced from a real scoring model
    if recent_avg > older_avg:
        value += 25.0
    if positive_share >= 0.5:
        value += 25.0
    detail = (f"{len(months)} month(s) observed; recent-half average net "
             f"{recent_avg:,.0f} vs earlier-half average {older_avg:,.0f}.")
    return _available(value, detail)


def _factor_debt(fe) -> dict:
    monthly_income = fe.effective_monthly_income()
    if monthly_income <= 0:
        return _unavailable("no income data to compute a debt-to-income ratio against")
    txns = fe.list_transactions(limit=5000)
    emi_total = sum(abs(t.get("amount") or 0) for t in txns if t.get("category") == "EMI/Loan")
    months = len({(t.get("date") or "")[:7] for t in txns if t.get("date")}) or 1
    monthly_emi = emi_total / months
    ratio = monthly_emi / monthly_income
    value = 100.0 - ratio * 200.0   # illustrative curve: 50% DTI -> 0
    detail = (f"'EMI/Loan'-categorized debits average ~{monthly_emi:,.0f}/mo vs "
             f"effective income ~{monthly_income:,.0f}/mo (~{ratio:.0%} DTI, illustrative).")
    return _available(value, detail)


def _factor_investment_profile(fe) -> dict:
    invs = fe.list_investments()
    note = ("Note: this schema has no account-balance data, so a distinct "
           "'savings' signal isn't computable — this factor is investments-only.")
    if not invs:
        return _available(30.0, f"No investments on file. {note}")
    total_value = sum(i.get("current_value") or 0 for i in invs)
    diversity = len({i.get("type") for i in invs if i.get("type")})
    value = 40.0 + 10.0 * min(diversity, 3)   # illustrative curve
    if total_value >= 1_000_000:
        value += 30.0
    elif total_value >= 300_000:
        value += 20.0
    elif total_value >= 50_000:
        value += 10.0
    detail = (f"{len(invs)} investment(s), {diversity} type(s), total value "
             f"~{total_value:,.0f}. {note}")
    return _available(value, detail)


def _factor_fraud_history(fe) -> dict:
    txns = fe.list_transactions(limit=5000)
    high = sum(1 for t in txns if t.get("fraud_risk_level") == "HIGH")
    critical = sum(1 for t in txns if t.get("fraud_risk_level") == "CRITICAL")
    value = 100.0 - 15.0 * high - 30.0 * critical   # illustrative curve
    detail = (f"{high} HIGH-risk and {critical} CRITICAL-risk fraud-scored "
             f"transaction(s) on file (amy/finance/fraud_engine.py).")
    return _available(value, detail)


def _factor_aml_alerts(fe) -> dict:
    cases = fe.list_aml_cases(limit=1000)
    open_count = sum(1 for c in cases if c["status"] in ("open", "investigating"))
    escalated = sum(1 for c in cases if c["status"] == "escalated")
    value = 100.0 - 15.0 * open_count - 30.0 * escalated   # illustrative curve
    detail = (f"{open_count} open/investigating and {escalated} escalated AML "
             f"case(s) on file (amy/finance/aml_engine.py).")
    return _available(value, detail)


def _factor_business_stability(fe) -> dict:
    from .business import entities as biz_entities

    ents = biz_entities.list_entities(fe)
    if not ents:
        return _unavailable("no business entity registered")
    all_entries = []
    for e in ents:
        all_entries += fe.list_ledger_entries(e["id"])
    if not all_entries:
        return _available(70.0, f"{len(ents)} business entit(y/ies) registered, no ledger entries yet.")
    flagged = sum(1 for x in all_entries if x.get("audit_status") == "flagged")
    value = 100.0 - 100.0 * flagged / len(all_entries)   # illustrative curve
    detail = (f"{len(ents)} business entit(y/ies), {len(all_entries)} ledger "
             f"entries, {flagged} flagged by the auditor.")
    return _available(value, detail)


# ---------------------------------------------------------------------------
# Aggregate score
# ---------------------------------------------------------------------------

def _build_explanation(factors: dict) -> str:
    prefix = "Amy Score — an internal signal, not a credit bureau score. "
    available_items = [(k, f) for k, f in factors.items() if f.get("available")]
    if not available_items:
        return prefix + "Not enough data on file yet to compute any factor."
    dragging = sorted(available_items, key=lambda kv: kv[1]["weight"] * (50 - kv[1]["value"]),
                      reverse=True)
    top_drag = [(k, f) for k, f in dragging if f["value"] < 50][:3]
    if top_drag:
        parts = [f"{_LABELS.get(k, k)} ({f['value']:.0f}/100)" for k, f in top_drag]
        return prefix + f"Main drags: {', '.join(parts)}."
    boosters = sorted(available_items, key=lambda kv: kv[1]["weight"] * kv[1]["value"],
                      reverse=True)[:3]
    parts = [f"{_LABELS.get(k, k)} ({f['value']:.0f}/100)" for k, f in boosters]
    return prefix + f"Strongest contributors: {', '.join(parts)}."


def _build_suggestions(factors: dict) -> list[str]:
    available_items = [(k, f) for k, f in factors.items() if f.get("available")]
    lowest = sorted(available_items, key=lambda kv: kv[1]["value"])[:3]
    return [_SUGGESTION_TEMPLATES[k] for k, f in lowest
           if k in _SUGGESTION_TEMPLATES and f["value"] < 90]


def compute_score(fe) -> dict:
    """Pure/read-only — builds the full score contract. Never persists
    anything; see record_score() below for that."""
    factors = {
        "payment_history": _factor_payment_history(fe),
        "income_stability": _factor_income_stability(fe),
        "cashflow_trend": _factor_cashflow_trend(fe),
        "debt": _factor_debt(fe),
        "investment_profile": _factor_investment_profile(fe),
        "overdrafts": _unavailable("no account-balance data is tracked in this schema "
                                   "— only transaction line items, no opening or running balance"),
        "fraud_history": _factor_fraud_history(fe),
        "aml_alerts": _factor_aml_alerts(fe),
        "business_stability": _factor_business_stability(fe),
        "bureau_score": _unavailable("no bureau integration in this system"),
    }
    weighted_sum = 0.0
    weight_total = 0.0
    for key, f in factors.items():
        w = WEIGHTS.get(key, 0)
        f["weight"] = w
        if f.get("available"):
            weighted_sum += w * f["value"]
            weight_total += w
    weighted_avg = (weighted_sum / weight_total) if weight_total > 0 else 50.0
    score = int(round(SCORE_FLOOR + (SCORE_CEIL - SCORE_FLOOR) * weighted_avg / 100.0))
    score = max(SCORE_FLOOR, min(SCORE_CEIL, score))
    return {
        "score": score,
        "computed_at": _now_iso(),
        "factors": factors,
        "explanation": _build_explanation(factors),
        "improvement_suggestions": _build_suggestions(factors),
    }


def record_score(ctx) -> dict:
    """Computes AND persists a credit_scores row + emits credit.updated.
    No submit_action/approval anywhere in this module — a derived read-
    model recompute over existing data, no external effect (same class of
    write as auto_categorize/budget suggestions elsewhere in this
    codebase), unlike Phase 1/2's detection tools which had real
    downstream consequence."""
    fe = ctx.open_finance()
    try:
        result = compute_score(fe)
        cid = fe.save_credit_score(result["score"], result["factors"],
                                   result["explanation"], result["improvement_suggestions"])
    finally:
        fe.close()
    try:
        ctx.events().emit("credit.updated", {
            "score": result["score"], "computed_at": result["computed_at"],
        }, source="credit_engine")
    except Exception:
        pass
    return {"id": cid, **result}
