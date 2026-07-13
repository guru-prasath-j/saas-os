"""Fraud Detection Module (Phase 1) — rule-based scorer + approval routing.

All transactions constructed in this file are SYNTHETIC test fixtures, not
real financial data. See amy/finance/fraud_engine.py's module docstring for
the "illustrative, not sourced from regulation" framing this module follows.
"""
import datetime as _dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.automation.executors import approve
from amy.collab import CollabDB
from amy.finance import fraud_engine
from amy.tools.registry import invoke as tool_invoke

# A fixed Monday, used as an anchor so weekday/weekend math in tests is
# deterministic regardless of when the suite runs.
_MONDAY = _dt.date(2026, 1, 5)
assert _MONDAY.weekday() == 0


def _d(offset_days: int) -> str:
    return (_MONDAY + _dt.timedelta(days=offset_days)).isoformat()


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-fraud", "fraud@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


# ---------------------------------------------------------------------------
# Score contract / risk bucketing
# ---------------------------------------------------------------------------

def test_risk_thresholds_are_contiguous_and_cover_0_to_100():
    covered = set()
    for _level, lo, hi in fraud_engine.RISK_THRESHOLDS:
        covered.update(range(lo, hi + 1))
    assert covered == set(range(0, 101))


def test_action_and_tier_defined_for_every_risk_level():
    levels = {lvl for lvl, _, _ in fraud_engine.RISK_THRESHOLDS}
    assert set(fraud_engine.ACTION_FOR_RISK) == levels
    assert set(fraud_engine.TIER_FOR_RISK) == levels
    # low/medium auto-apply, high/critical always require a human
    assert fraud_engine.TIER_FOR_RISK["LOW"] < 2
    assert fraud_engine.TIER_FOR_RISK["MEDIUM"] < 2
    assert fraud_engine.TIER_FOR_RISK["HIGH"] == 2
    assert fraud_engine.TIER_FOR_RISK["CRITICAL"] == 2


def test_time_of_day_is_honestly_marked_unavailable(ctx):
    """transactions.date has no time component (see fraud_engine's module
    docstring) — a literal time-of-day signal must not be fabricated."""
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        fe.add_transaction(-100, "Food", "Cafe X", date=_d(0), account_id=aid)
        tid = fe.add_transaction(-100, "Food", "Cafe X", date=_d(1), account_id=aid)
        score = fraud_engine.score_transaction(fe, tid)
    finally:
        fe.close()
    assert "time_of_day_anomaly" in score["unavailable_signals"]
    assert all(s["reason_code"] != "time_of_day_anomaly" for s in score["signals"])


# ---------------------------------------------------------------------------
# Clean case
# ---------------------------------------------------------------------------

def test_clean_transaction_scores_low_with_no_reason_codes(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        # prior history so this ISN'T a first-time counterparty
        fe.add_transaction(-437, "Food", "Everyday Store", date=_d(-10), account_id=aid)
        tid = fe.add_transaction(-412, "Food", "Everyday Store", date=_d(0), account_id=aid)
        score = fraud_engine.score_transaction(fe, tid)
    finally:
        fe.close()
    assert score["score"] == 0
    assert score["risk_level"] == "LOW"
    assert score["reason_codes"] == []
    assert score["recommended_action"] == "allow"
    assert "No rule-based fraud signals" in score["explanation"]


# ---------------------------------------------------------------------------
# Individual signals
# ---------------------------------------------------------------------------

def test_velocity_signal_triggers_on_same_day_burst(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        for i in range(3):
            fe.add_transaction(-50, "Food", f"Shop {i}", date=_d(0), account_id=aid)
        tid = fe.add_transaction(-50, "Food", "Shop 4", date=_d(0), account_id=aid)
        score = fraud_engine.score_transaction(fe, tid)
    finally:
        fe.close()
    assert "velocity_spike" in score["reason_codes"]


def test_round_number_signal_triggers_on_large_round_amount(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        tid = fe.add_transaction(-20000, "Transfer", "Some Payee", date=_d(0), account_id=aid)
        score = fraud_engine.score_transaction(fe, tid)
    finally:
        fe.close()
    assert "round_number_amount" in score["reason_codes"]


def test_round_number_signal_does_not_trigger_on_ordinary_amount(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        tid = fe.add_transaction(-1234, "Food", "Cafe", date=_d(0), account_id=aid)
        score = fraud_engine.score_transaction(fe, tid)
    finally:
        fe.close()
    assert "round_number_amount" not in score["reason_codes"]


def test_spend_spike_signal_triggers_vs_own_history(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        for i in range(4):
            fe.add_transaction(-500, "Food", "Regular Cafe", date=_d(-20 - i), account_id=aid)
        tid = fe.add_transaction(-50000, "Transfer", "Big One-off", date=_d(0), account_id=aid)
        score = fraud_engine.score_transaction(fe, tid)
    finally:
        fe.close()
    assert "spend_spike_vs_own_average" in score["reason_codes"]


def test_new_beneficiary_signal_triggers_on_freshly_added_beneficiary(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Custodial", "Test Bank", account_type="custodial")
        bid = fe.add_beneficiary(aid, "New Person")
        # add_beneficiary always stamps real wall-clock created_at — pin it
        # to the same synthetic date as the transaction so the gap is 0,
        # regardless of when this test actually runs.
        fe.conn.execute("UPDATE beneficiaries SET created_at=? WHERE id=?", (_d(0), bid))
        tid = fe.add_transaction(-3000, "Custodial Disbursement", "New Person",
                                 date=_d(0), account_id=aid)
        fe.conn.execute("UPDATE transactions SET beneficiary_id=? WHERE id=?", (bid, tid))
        fe.conn.commit()
        score = fraud_engine.score_transaction(fe, tid)
    finally:
        fe.close()
    assert "new_beneficiary" in score["reason_codes"]


def test_first_time_counterparty_triggers_for_ordinary_new_payee(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        fe.add_transaction(-200, "Food", "Known Merchant", date=_d(-5), account_id=aid)
        tid = fe.add_transaction(-200, "Shopping", "Brand New Merchant",
                                 date=_d(0), account_id=aid)
        score = fraud_engine.score_transaction(fe, tid)
    finally:
        fe.close()
    assert "first_time_counterparty" in score["reason_codes"]


def test_dormant_reactivation_signal_triggers_on_large_gap_and_amount(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        fe.add_transaction(-100, "Food", "Old Shop", date=_d(-100), account_id=aid)
        tid = fe.add_transaction(-9000, "Transfer", "Old Shop", date=_d(0), account_id=aid)
        score = fraud_engine.score_transaction(fe, tid)
    finally:
        fe.close()
    assert "dormant_account_reactivation" in score["reason_codes"]


def test_atypical_day_signal_triggers_on_weekend_for_weekday_only_account(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        # 12 prior transactions, every one on a weekday (offsets 0..11 from
        # the Monday anchor land on weekdays for the first 5, then repeat
        # — use every 7-day step to stay on Mondays).
        for i in range(12):
            fe.add_transaction(-50, "Food", "Weekday Shop", date=_d(-7 * (i + 1)), account_id=aid)
        # this transaction lands on the following Saturday (+5 days)
        tid = fe.add_transaction(-50, "Food", "Weekday Shop", date=_d(5), account_id=aid)
        score = fraud_engine.score_transaction(fe, tid)
    finally:
        fe.close()
    assert "atypical_day_of_week" in score["reason_codes"]


# ---------------------------------------------------------------------------
# review_transaction() tier routing
# ---------------------------------------------------------------------------

def test_low_risk_review_auto_executes_and_persists_immediately(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        fe.add_transaction(-437, "Food", "Everyday Store", date=_d(-10), account_id=aid)
        tid = fe.add_transaction(-412, "Food", "Everyday Store", date=_d(0), account_id=aid)
    finally:
        fe.close()

    result = fraud_engine.review_transaction(ctx, tid)
    assert result["risk_level"] == "LOW"
    assert result["approval"]["status"] == "auto_executed"

    fe = ctx.open_finance()
    try:
        stored = fe.get_fraud_score(tid)
    finally:
        fe.close()
    assert stored is not None
    assert stored["fraud_risk_level"] == "LOW"


def test_high_risk_review_parks_pending_approval_and_does_not_persist_yet(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        for i in range(4):
            fe.add_transaction(-500, "Food", "Regular Cafe", date=_d(-20 - i), account_id=aid)
        # round number + spend spike + first-time counterparty => HIGH (65)
        tid = fe.add_transaction(-50000, "Transfer", "Suspicious New Payee",
                                 date=_d(0), account_id=aid)
    finally:
        fe.close()

    result = fraud_engine.review_transaction(ctx, tid)
    assert result["risk_level"] == "HIGH"
    assert result["approval"]["status"] == "pending"
    approval_id = result["approval"]["approval_id"]

    pending = ctx.store.list_approvals("pending")
    assert len(pending) == 1
    assert pending[0]["id"] == approval_id
    assert pending[0]["payload"]["transaction_id"] == tid
    assert pending[0]["dedup_key"] == f"fraud_{tid}_HIGH"
    assert pending[0]["risk"] == "write"   # HIGH, not CRITICAL — see fraud_engine.review_transaction

    # not silently blocked/persisted before a human decides
    fe = ctx.open_finance()
    try:
        assert fe.get_fraud_score(tid) is None
    finally:
        fe.close()

    # approving executes the parked action
    out = approve(ctx, approval_id)
    assert out["status"] == "executed"

    fe = ctx.open_finance()
    try:
        stored = fe.get_fraud_score(tid)
    finally:
        fe.close()
    assert stored is not None
    assert stored["fraud_risk_level"] == "HIGH"
    assert "round_number_amount" in stored["fraud_reason_codes"]


def test_rescoring_after_severity_change_is_not_deduped_against_stale_approval(ctx):
    """dedup_key includes risk_level specifically so a transaction that was
    LOW yesterday and is HIGH today (e.g. after a beneficiary/merchant
    change) still gets a fresh approval rather than being silently
    swallowed by create_approval's dedup."""
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        tid = fe.add_transaction(-100, "Food", "Cafe", date=_d(0), account_id=aid)
    finally:
        fe.close()
    low = fraud_engine.review_transaction(ctx, tid)
    assert low["risk_level"] == "LOW"

    # Force a different transaction into a HIGH score by giving it distinct
    # history — this stands in for "rescored later with more signal data."
    fe = ctx.open_finance()
    try:
        aid2 = fe.add_account("Checking2", "Test Bank", account_type="savings")
        for i in range(4):
            fe.add_transaction(-500, "Food", "Regular Cafe", date=_d(-20 - i), account_id=aid2)
        tid2 = fe.add_transaction(-50000, "Transfer", "Suspicious New Payee",
                                  date=_d(0), account_id=aid2)
    finally:
        fe.close()
    high = fraud_engine.review_transaction(ctx, tid2)
    assert high["risk_level"] == "HIGH"
    assert high["approval"]["status"] == "pending"


# ---------------------------------------------------------------------------
# Registry tools
# ---------------------------------------------------------------------------

def test_score_fraud_risk_tool_matches_direct_call(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        tid = fe.add_transaction(-20000, "Transfer", "Some Payee", date=_d(0), account_id=aid)
        direct = fraud_engine.score_transaction(fe, tid)
    finally:
        fe.close()
    via_tool = tool_invoke(ctx, "score_fraud_risk", {"transaction_id": tid}, actor="human")
    assert via_tool["score"] == direct["score"]
    assert via_tool["reason_codes"] == direct["reason_codes"]


def test_explain_fraud_score_is_honest_before_and_after_review(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        fe.add_transaction(-437, "Food", "Everyday Store", date=_d(-10), account_id=aid)
        tid = fe.add_transaction(-412, "Food", "Everyday Store", date=_d(0), account_id=aid)
    finally:
        fe.close()

    before = tool_invoke(ctx, "explain_fraud_score", {"transaction_id": tid}, actor="human")
    assert before["available"] is False

    fraud_engine.review_transaction(ctx, tid)   # LOW — auto-persists

    after = tool_invoke(ctx, "explain_fraud_score", {"transaction_id": tid}, actor="human")
    assert after["available"] is True
    assert after["fraud_risk_level"] == "LOW"
