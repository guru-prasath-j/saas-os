"""Fraud Detection Module (Phase 1) — rule-based transaction risk scoring.

ILLUSTRATIVE / SIMULATED ONLY. This is a personal-finance portfolio project
(Amy PersonalOS), not a real banking fraud system: there is no fraud-labeled
training dataset, no sanctions/PEP list, and no bureau feed anywhere in this
codebase. Every threshold below is a placeholder chosen for a plausible demo,
not sourced from any regulation, card-network rule, or real fraud dataset —
each is commented `# illustrative threshold, not sourced from regulation`.
Never present a number this module produces as a real risk assessment.

Follows the same honesty convention already established elsewhere in this
codebase (the company_intel/health_data stubs in amy/career_apply.py /
amy/life/health_data.py, which return `available:false` instead of inventing
data): signals this system has no data source for are named in
UNAVAILABLE_SIGNALS and surfaced in the score output, never faked.

One real schema constraint shapes two of the six signals originally scoped:
`transactions.date` is a date-only "YYYY-MM-DD" string across every import
path (CSV/PDF/Gmail — see amy/finance/sync/*.py) with no time-of-day
component. A literal "transactions at 3am" signal is therefore not
computable from real data, so:
  - "time-of-day anomaly" is listed in UNAVAILABLE_SIGNALS rather than
    faked with an invented hour, and
  - the closest honestly-computable substitute — atypical day-of-week for
    this account (day-of-week IS derivable from a date string) — is
    implemented instead (`atypical_day_of_week`).
  - the velocity signal is day-granularity ("N transactions on the same
    calendar day"), not hour-granularity, for the same reason.

This module only computes scores — see FinanceEngine.save_fraud_score/
get_fraud_score for persistence, and review_transaction() below for how a
score gets routed through the existing tier-2 approval pipeline.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Illustrative thresholds — every one is a placeholder, none sourced from
# regulation, a card-network rule, or a real fraud dataset.
# ---------------------------------------------------------------------------

VELOCITY_COUNT_THRESHOLD = 4        # illustrative threshold, not sourced from regulation
ROUND_NUMBER_FLOOR = 10_000.0       # illustrative threshold, not sourced from regulation
SPEND_SPIKE_MULTIPLIER = 4.0        # illustrative threshold, not sourced from regulation
SPEND_SPIKE_FLOOR = 5_000.0         # illustrative threshold, not sourced from regulation
NEW_BENEFICIARY_WINDOW_DAYS = 3     # illustrative threshold, not sourced from regulation
DORMANT_GAP_DAYS = 90               # illustrative threshold, not sourced from regulation
DORMANT_LARGE_FLOOR = 5_000.0       # illustrative threshold, not sourced from regulation
ATYPICAL_DAY_MIN_HISTORY = 10       # illustrative threshold, not sourced from regulation
ATYPICAL_WEEKEND_SHARE_FLOOR = 0.10 # illustrative threshold, not sourced from regulation
LOOKBACK_DAYS = 180                 # history window the comparison signals draw from

# score bucket -> (low, high) inclusive; illustrative, not sourced from regulation
RISK_THRESHOLDS = (
    ("LOW", 0, 24),
    ("MEDIUM", 25, 49),
    ("HIGH", 50, 74),
    ("CRITICAL", 75, 100),
)

ACTION_FOR_RISK = {
    "LOW": "allow",
    "MEDIUM": "require_mfa",
    "HIGH": "hold",
    "CRITICAL": "block",
}

# Tier for amy.automation.executors.submit_action. LOW/MEDIUM auto-apply
# (tier 0/1 — annotate the transaction, tier 1 also notifies); HIGH/CRITICAL
# always park as a tier-2 human approval, per the "never auto-block
# silently" requirement. Applied directly by review_transaction() below,
# NOT through the generic per-tool-name AGENT_GATE tiering in
# amy/tools/registry.py — AGENT_GATE only fires for actor="agent" calls and
# assigns one static tier per tool name, so it can't vary by THIS score's
# severity. See amy/tools/fraud_tools.py's module docstring for the full
# reasoning; don't "simplify" this into relying on AGENT_GATE alone.
TIER_FOR_RISK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 2}

# Signals this rule-based scorer does NOT compute, and why. No invented
# values, ever — surfaced verbatim in score_transaction()'s output.
UNAVAILABLE_SIGNALS = {
    "device_fingerprint": "no device/session data is captured anywhere in this app",
    "impossible_travel": "no login/session geo data exists — amy/geo/ tracks place "
                          "visits for Life Autopilot, not login IPs or sessions",
    "login_anomaly": "there is no login-event log in this codebase",
    "merchant_category_code": "imported transactions carry a free-text merchant "
                               "string (see amy/finance/sync/*.py), not an MCC",
    "time_of_day_anomaly": "transactions.date is a date-only 'YYYY-MM-DD' string "
                            "with no time component across every import path — see "
                            "'atypical_day_of_week' for the honest day-of-week substitute",
}

_REASON_LABELS = {
    "velocity_spike": "unusually many transactions on this account today",
    "round_number_amount": "a suspiciously round large amount",
    "spend_spike_vs_own_average": "a large spike vs. this account's own spending history",
    "new_beneficiary": "sent to a beneficiary added just before this transaction",
    "first_time_counterparty": "a first-time payment to this counterparty on this account",
    "dormant_account_reactivation": "reactivation of a long-dormant account with a large amount",
    "atypical_day_of_week": "activity on a day this account rarely transacts on",
}


@dataclass
class SignalResult:
    reason_code: str
    triggered: bool
    weight: int          # points added to the score when triggered
    detail: str           # always populated, whether triggered or not


def _parse_date(date_str) -> _dt.date | None:
    if not date_str:
        return None
    try:
        return _dt.date.fromisoformat(str(date_str)[:10])
    except ValueError:
        return None


def _risk_level_for_score(score: int) -> str:
    for level, lo, hi in RISK_THRESHOLDS:
        if lo <= score <= hi:
            return level
    return "CRITICAL"


# ---------------------------------------------------------------------------
# Signals — each always returns a SignalResult (triggered or not), so the
# full evaluation is visible in the score output, not just what fired.
# ---------------------------------------------------------------------------

def _velocity_signal(fe, txn: dict) -> SignalResult:
    reason_code = "velocity_spike"
    account_id = txn.get("account_id")
    date = _parse_date(txn.get("date"))
    if not account_id or date is None:
        return SignalResult(reason_code, False, 25, "account_id or date missing — not evaluated")
    same_day = [t for t in fe.list_transactions(limit=200, account_id=account_id,
                                                since=date.isoformat(), until=date.isoformat())
                if t["id"] != txn["id"]]
    count = len(same_day) + 1   # + this transaction itself
    triggered = count >= VELOCITY_COUNT_THRESHOLD
    detail = (f"{count} transaction(s) on this account on {date.isoformat()} "
              f"(threshold {VELOCITY_COUNT_THRESHOLD}). Day-granularity only — "
              "transactions.date has no time-of-day component in this schema, so a "
              "true rolling-hour velocity check isn't possible here.")
    return SignalResult(reason_code, triggered, 25, detail)


def _round_number_signal(fe, txn: dict) -> SignalResult:
    reason_code = "round_number_amount"
    amount = abs(txn.get("amount") or 0)
    triggered = amount >= ROUND_NUMBER_FLOOR and amount % 1000 == 0
    detail = (f"amount {amount:,.0f} is an exact multiple of 1,000 at or above "
              f"{ROUND_NUMBER_FLOOR:,.0f}" if triggered else
              f"amount {amount:,.0f} is not a suspiciously round large figure")
    return SignalResult(reason_code, triggered, 15, detail)


def _spend_spike_signal(fe, txn: dict) -> SignalResult:
    reason_code = "spend_spike_vs_own_average"
    amount = txn.get("amount") or 0
    if amount >= 0:
        return SignalResult(reason_code, False, 30, "credit, not a debit — not evaluated")
    account_id = txn.get("account_id")
    date = _parse_date(txn.get("date"))
    since = (date - _dt.timedelta(days=LOOKBACK_DAYS)).isoformat() if date else None
    until = date.isoformat() if date else None
    history = [t for t in fe.list_transactions(limit=2000, account_id=account_id,
                                               since=since, until=until)
               if t["id"] != txn["id"] and (t["amount"] or 0) < 0]
    if len(history) < 3:
        return SignalResult(reason_code, False, 30,
                            "fewer than 3 prior debits on this account — not enough history to compare")
    avg = sum(abs(t["amount"]) for t in history) / len(history)
    triggered = abs(amount) >= max(SPEND_SPIKE_FLOOR, SPEND_SPIKE_MULTIPLIER * avg)
    detail = (f"{abs(amount):,.0f} vs this account's trailing {len(history)}-debit "
              f"average of {avg:,.0f} ({SPEND_SPIKE_MULTIPLIER:.0f}x threshold)")
    return SignalResult(reason_code, triggered, 30, detail)


def _new_beneficiary_signal(fe, txn: dict) -> SignalResult:
    beneficiary_id = txn.get("beneficiary_id")
    date = _parse_date(txn.get("date"))
    if beneficiary_id:
        row = fe.conn.execute("SELECT created_at FROM beneficiaries WHERE id=?",
                              (beneficiary_id,)).fetchone()
        created = _parse_date(row["created_at"]) if row else None
        if created and date:
            gap = (date - created).days
            triggered = 0 <= gap <= NEW_BENEFICIARY_WINDOW_DAYS
            detail = (f"beneficiary added {gap} day(s) before this disbursement"
                      if triggered else
                      f"beneficiary on file for {gap} day(s) before this disbursement")
            return SignalResult("new_beneficiary", triggered, 20, detail)
        return SignalResult("new_beneficiary", False, 20,
                            "beneficiary_id set but its creation date is unavailable")
    # Non-custodial fallback: first-ever payment to this counterparty (by
    # merchant string) on this account. The beneficiaries table only covers
    # custodial disbursements (see CLAUDE.md's custodial section) — most
    # personal transactions have no beneficiary_id at all.
    account_id = txn.get("account_id")
    merchant = (txn.get("merchant") or "").strip()
    if not account_id or not merchant:
        return SignalResult("first_time_counterparty", False, 20,
                            "no account_id/merchant to compare")
    prior = [t for t in fe.list_transactions(limit=2000, account_id=account_id)
             if t["id"] != txn["id"] and (t.get("merchant") or "").strip().lower() == merchant.lower()]
    triggered = len(prior) == 0
    detail = (f"no prior transaction to '{merchant}' on this account" if triggered
              else f"{len(prior)} prior transaction(s) to '{merchant}' on this account")
    return SignalResult("first_time_counterparty", triggered, 20, detail)


def _dormant_reactivation_signal(fe, txn: dict) -> SignalResult:
    reason_code = "dormant_account_reactivation"
    account_id = txn.get("account_id")
    date = _parse_date(txn.get("date"))
    amount = abs(txn.get("amount") or 0)
    if not account_id or date is None:
        return SignalResult(reason_code, False, 25, "account_id or date missing — not evaluated")
    prior = [t for t in fe.list_transactions(limit=2000, account_id=account_id, until=date.isoformat())
             if t["id"] != txn["id"]]
    prior_dates = [d for d in (_parse_date(t["date"]) for t in prior) if d]
    if not prior_dates:
        return SignalResult(reason_code, False, 25, "no prior transaction history on this account")
    last_date = max(prior_dates)
    gap = (date - last_date).days
    triggered = gap >= DORMANT_GAP_DAYS and amount >= DORMANT_LARGE_FLOOR
    detail = f"{gap} day gap since the previous transaction on this account, amount {amount:,.0f}"
    return SignalResult(reason_code, triggered, 25, detail)


def _atypical_day_signal(fe, txn: dict) -> SignalResult:
    """Honest substitute for a literal time-of-day check — see the module
    docstring for why transactions.date can't support one."""
    reason_code = "atypical_day_of_week"
    date = _parse_date(txn.get("date"))
    account_id = txn.get("account_id")
    if date is None or not account_id:
        return SignalResult(reason_code, False, 10, "date or account_id missing — not evaluated")
    history = [t for t in fe.list_transactions(limit=2000, account_id=account_id)
               if t["id"] != txn["id"]]
    if len(history) < ATYPICAL_DAY_MIN_HISTORY:
        return SignalResult(reason_code, False, 10,
                            f"fewer than {ATYPICAL_DAY_MIN_HISTORY} prior transactions — "
                            "not enough history to establish a day-of-week baseline")
    is_weekend = date.weekday() >= 5
    weekend_count = sum(1 for d in (_parse_date(t["date"]) for t in history) if d and d.weekday() >= 5)
    weekend_share = weekend_count / len(history)
    triggered = is_weekend and weekend_share < ATYPICAL_WEEKEND_SHARE_FLOOR
    detail = (f"weekend transaction; only {weekend_share:.0%} of this account's "
              f"{len(history)} prior transactions were on a weekend" if is_weekend
              else "not a weekend transaction")
    return SignalResult(reason_code, triggered, 10, detail)


_SIGNAL_FUNCS = (
    _velocity_signal, _round_number_signal, _spend_spike_signal,
    _new_beneficiary_signal, _dormant_reactivation_signal, _atypical_day_signal,
)


def _confidence_for(fe, txn: dict) -> float:
    """Reflects how much historical data grounds the comparison-based
    signals (spend-spike / dormant-gap / atypical-day) — NOT a calibrated
    fraud probability. No trained model exists to produce a real
    probability (see module docstring); a sparse-history account can still
    score HIGH on velocity/round-number/new-beneficiary alone, just with
    less statistical grounding behind the comparison signals."""
    account_id = txn.get("account_id")
    if not account_id:
        return 0.3
    n = fe.conn.execute(
        "SELECT COUNT(*) n FROM transactions WHERE account_id=? AND id!=?",
        (account_id, txn["id"])).fetchone()["n"]
    if n >= 30:
        return 0.9
    if n >= 10:
        return 0.7
    if n >= 3:
        return 0.5
    return 0.3


def _explain(triggered: list[SignalResult], risk_level: str) -> str:
    if not triggered:
        return "No rule-based fraud signals were triggered for this transaction."
    labels = [_REASON_LABELS.get(s.reason_code, s.reason_code) for s in triggered]
    return f"{risk_level} risk — flagged for " + "; ".join(labels) + "."


def score_transaction(fe, transaction_id: str) -> dict:
    """Pure/read-only: evaluates every signal and returns the score
    contract. Never writes to the database — see FinanceEngine.save_fraud_score
    for persistence and review_transaction() below for approval routing."""
    txn = fe.get_transaction(transaction_id)
    if txn is None:
        raise ValueError(f"transaction {transaction_id!r} not found")
    signals = [fn(fe, txn) for fn in _SIGNAL_FUNCS]
    triggered = [s for s in signals if s.triggered]
    score = min(100, sum(s.weight for s in triggered))
    risk_level = _risk_level_for_score(score)
    return {
        "transaction_id": transaction_id,
        "score": score,
        "risk_level": risk_level,
        "reason_codes": [s.reason_code for s in triggered],
        "confidence": _confidence_for(fe, txn),
        "recommended_action": ACTION_FOR_RISK[risk_level],
        "explanation": _explain(triggered, risk_level),
        "signals": [dataclasses.asdict(s) for s in signals],
        "unavailable_signals": dict(UNAVAILABLE_SIGNALS),
    }


def review_transaction(ctx, transaction_id: str) -> dict:
    """Score a transaction, then route it through the existing tier
    router (amy.automation.executors.submit_action) — LOW/MEDIUM apply
    immediately (tier 0/1), HIGH/CRITICAL park as a tier-2 approval that
    only takes effect once a human approves it in the Approval Inbox.

    Calls submit_action directly with an explicitly-computed tier instead
    of going through amy.tools.registry.AGENT_GATE's static per-tool-name
    tiering — the same "severity computed per-call, not per-tool" pattern
    already used in amy/agents/reactive.py for resume_update and
    career_wind_down. See amy/tools/fraud_tools.py for the registry-tool
    wrapper around this function.
    """
    from ..automation.executors import submit_action

    fe = ctx.open_finance()
    try:
        score = score_transaction(fe, transaction_id)
    finally:
        fe.close()

    tier = TIER_FOR_RISK[score["risk_level"]]
    result = submit_action(
        ctx, tier, "fraud_review_action",
        title=f"Fraud review — {score['risk_level']}: transaction {transaction_id}",
        body=score["explanation"],
        payload={"transaction_id": transaction_id, "score": score},
        source="fraud_engine",
        dedup_key=f"fraud_{transaction_id}_{score['risk_level']}",
        reasoning=score["explanation"],
        risk="destructive" if score["risk_level"] == "CRITICAL" else "write",
        affected_entity=f"transaction_id={transaction_id}")
    return {**score, "approval": result}
