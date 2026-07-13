"""Amy Credit Score Module (Phase 3) — illustrative internal score, never a
real credit bureau score.

All accounts/transactions/income constructed in this file are SYNTHETIC
test fixtures, not real financial data. See amy/finance/credit_engine.py's
module docstring for the "Amy Score, not a credit bureau score" framing
and the two honesty notes this module follows (payment_history is a
proxy; overdrafts have no account-balance data to draw on).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.collab import CollabDB
from amy.events.store import EventStore
from amy.finance import credit_engine
from amy.tools.registry import invoke as tool_invoke


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-credit", "credit@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


# ---------------------------------------------------------------------------
# Always-unavailable factors
# ---------------------------------------------------------------------------

def test_bureau_score_and_overdrafts_always_unavailable(ctx):
    fe = ctx.open_finance()
    try:
        result = credit_engine.compute_score(fe)
    finally:
        fe.close()
    assert result["factors"]["bureau_score"]["available"] is False
    assert "bureau" in result["factors"]["bureau_score"]["reason"].lower()
    assert result["factors"]["overdrafts"]["available"] is False
    assert "balance" in result["factors"]["overdrafts"]["reason"].lower()


# ---------------------------------------------------------------------------
# Partial-data profile — the prompt's explicit "doesn't error" requirement
# ---------------------------------------------------------------------------

def test_empty_profile_still_computes_a_sensible_score(ctx):
    fe = ctx.open_finance()
    try:
        result = credit_engine.compute_score(fe)
    finally:
        fe.close()
    assert 300 <= result["score"] <= 900
    for key in ("income_stability", "cashflow_trend", "debt",
               "business_stability", "overdrafts", "bureau_score"):
        assert result["factors"][key]["available"] is False
    # a factor with no records is still a real (if minimal) answer, not "unavailable"
    assert result["factors"]["investment_profile"]["available"] is True
    assert result["factors"]["fraud_history"]["available"] is True
    assert result["factors"]["aml_alerts"]["available"] is True
    assert result["factors"]["payment_history"]["available"] is True
    assert "Amy Score" in result["explanation"]


def test_suggestions_never_reference_unavailable_factors(ctx):
    fe = ctx.open_finance()
    try:
        result = credit_engine.compute_score(fe)
    finally:
        fe.close()
    unavailable = {k for k, f in result["factors"].items() if not f.get("available")}
    for key in unavailable:
        template = credit_engine._SUGGESTION_TEMPLATES.get(key)
        if template:
            assert template not in result["improvement_suggestions"]


# ---------------------------------------------------------------------------
# Clean high-score profile
# ---------------------------------------------------------------------------

def test_clean_healthy_profile_scores_high(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        fe.add_income_source("Salary", "salary", 50000, "monthly")
        for i in range(1, 5):
            fe.add_transaction(50000, "Income", "Salary", date=f"2026-0{i}-01", account_id=aid)
            fe.add_transaction(-15000, "Food", "Groceries", date=f"2026-0{i}-15", account_id=aid)
        fe.add_investment("mutual_fund", "Index Fund", current_value=600000, cost_basis=500000)
        fe.add_investment("stocks", "Blue Chip", current_value=200000, cost_basis=150000)
        result = credit_engine.compute_score(fe)
    finally:
        fe.close()
    assert result["score"] >= 650
    assert "Amy Score — an internal signal, not a credit bureau score." in result["explanation"]


# ---------------------------------------------------------------------------
# Fraud/AML flags drag the score down
# ---------------------------------------------------------------------------

def test_fraud_and_aml_flags_drag_score_down(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        fe.add_income_source("Salary", "salary", 50000, "monthly")
        for i in range(1, 5):
            fe.add_transaction(50000, "Income", "Salary", date=f"2026-0{i}-01", account_id=aid)
            fe.add_transaction(-15000, "Food", "Groceries", date=f"2026-0{i}-15", account_id=aid)
        baseline = credit_engine.compute_score(fe)

        tid = fe.add_transaction(-9000, "Transfer", "Suspicious Payee",
                                 date="2026-05-01", account_id=aid)
        fe.save_fraud_score(tid, {"risk_level": "CRITICAL", "score": 90,
                                  "recommended_action": "block",
                                  "reason_codes": ["round_number_amount"]})
        case_id = fe.create_aml_case(aid, "structuring", "HIGH", 60, [tid], [],
                                     "synthetic test case")
        fe.update_aml_case_status(case_id, "escalated")

        dirty = credit_engine.compute_score(fe)
    finally:
        fe.close()
    assert dirty["score"] < baseline["score"]
    assert dirty["factors"]["fraud_history"]["value"] < 100
    assert dirty["factors"]["aml_alerts"]["value"] < 100


# ---------------------------------------------------------------------------
# record_score() persistence + event
# ---------------------------------------------------------------------------

def test_record_score_persists_history_and_emits_event(ctx):
    result = credit_engine.record_score(ctx)

    fe = ctx.open_finance()
    try:
        history = fe.list_credit_score_history()
        latest = fe.get_latest_credit_score()
    finally:
        fe.close()
    assert len(history) == 1
    assert history[0]["score"] == result["score"]
    assert latest["score"] == result["score"]

    es = EventStore(ctx.collab)
    events = es.recent("credit.updated")
    assert len(events) == 1
    assert events[0]["payload"]["score"] == result["score"]


# ---------------------------------------------------------------------------
# Registry tools
# ---------------------------------------------------------------------------

def test_explain_and_improve_are_honest_before_and_after_compute(ctx):
    before_explain = tool_invoke(ctx, "explain_credit_score", {}, actor="human")
    assert before_explain["available"] is False
    before_improve = tool_invoke(ctx, "improve_credit_score", {}, actor="human")
    assert before_improve["available"] is False

    tool_invoke(ctx, "compute_credit_score", {}, actor="human")

    after_explain = tool_invoke(ctx, "explain_credit_score", {}, actor="human")
    assert after_explain["available"] is True
    assert "Amy Score" in after_explain["explanation"]

    after_improve = tool_invoke(ctx, "improve_credit_score", {}, actor="human")
    assert after_improve["available"] is True
    assert isinstance(after_improve["suggestions"], list)


def test_credit_score_history_tool(ctx):
    tool_invoke(ctx, "compute_credit_score", {}, actor="human")
    tool_invoke(ctx, "compute_credit_score", {}, actor="human")
    out = tool_invoke(ctx, "credit_score_history", {}, actor="human")
    assert len(out["history"]) == 2
