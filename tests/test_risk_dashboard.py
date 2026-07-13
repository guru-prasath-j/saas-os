"""Compliance/Risk Dashboard (Phase 6) — read-only aggregation over
Phases 1-5. All rows seeded in this file are SYNTHETIC test fixtures,
seeded directly via FinanceEngine CRUD (not re-run through the Phase
1-5 detection pipelines — this phase is aggregation, not detection).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.collab import CollabDB
from amy.saas.routers.risk_dashboard import (
    _aml_summary, _credit_summary, _executive_summary, _explain,
    _fraud_summary, _loan_summary,
)


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-riskdash", "riskdash@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


_MIN_DECISION = {"recommended_rate": 0.1, "emi": 1000.0,
                 "explanation": {"credit_score_used": None}}


def _seed_fraud(fe, aid):
    ids = {}
    for lvl, action in (("LOW", "allow"), ("MEDIUM", "require_mfa"),
                        ("HIGH", "hold"), ("CRITICAL", "block")):
        tid = fe.add_transaction(-1000, "Food", f"Synthetic {lvl}",
                                 date="2026-01-01", account_id=aid)
        fe.save_fraud_score(tid, {"score": 50, "risk_level": lvl,
                                  "recommended_action": action, "reason_codes": []})
        ids[lvl] = tid
    return ids


# ---------------------------------------------------------------------------
# Empty dataset — honest None, never fabricated
# ---------------------------------------------------------------------------

def test_empty_dataset_is_honest_not_fabricated(ctx):
    fe = ctx.open_finance()
    try:
        fraud = _fraud_summary(fe)
        aml = _aml_summary(fe)
        credit = _credit_summary(fe)
        loans = _loan_summary(fe, ctx.store)
    finally:
        fe.close()
    assert fraud["total_scored"] == 0
    assert fraud["fraud_rate"] is None
    assert aml["total_cases"] == 0
    assert credit["latest"] is None
    assert credit["trend"] is None
    assert loans["total_applications"] == 0
    assert loans["approval_rate"] is None


# ---------------------------------------------------------------------------
# Fraud
# ---------------------------------------------------------------------------

def test_fraud_summary_matches_seeded_rows(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        _seed_fraud(fe, aid)
        summary = _fraud_summary(fe)
    finally:
        fe.close()
    assert summary["total_scored"] == 4
    assert summary["risk_level_counts"] == {"LOW": 1, "MEDIUM": 1, "HIGH": 1, "CRITICAL": 1}
    assert summary["blocked_count"] == 1
    assert summary["fraud_rate"] == 0.5   # (HIGH+CRITICAL)/total = 2/4
    assert len(summary["recent_flagged"]) == 3   # LOW excluded by list_flagged_transactions


# ---------------------------------------------------------------------------
# AML
# ---------------------------------------------------------------------------

def test_aml_summary_matches_seeded_rows(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        c1 = fe.create_aml_case(aid, "structuring", "HIGH", 60, ["t1"], [], "synthetic")
        c2 = fe.create_aml_case(aid, "layering", "MEDIUM", 40, ["t2"], [], "synthetic")
        c3 = fe.create_aml_case(aid, "structuring", "HIGH", 65, ["t3"], [], "synthetic")
        fe.update_aml_case_status(c3, "escalated")
        c4 = fe.create_aml_case(aid, "cash_spike", "LOW", 20, ["t4"], [], "synthetic")
        fe.update_aml_case_status(c4, "closed")
        summary = _aml_summary(fe)
    finally:
        fe.close()
    assert summary["total_cases"] == 4
    assert summary["status_counts"] == {"open": 2, "escalated": 1, "closed": 1}
    assert summary["typology_counts"] == {"structuring": 2, "layering": 1, "cash_spike": 1}
    assert summary["open_or_escalated"] == 3


# ---------------------------------------------------------------------------
# Credit
# ---------------------------------------------------------------------------

def test_credit_summary_trend_improving(ctx):
    fe = ctx.open_finance()
    try:
        fe.save_credit_score(700, {}, "synthetic first score", [])
        fe.save_credit_score(750, {}, "synthetic second score", [])
        summary = _credit_summary(fe)
    finally:
        fe.close()
    assert summary["latest"]["score"] == 750
    assert summary["trend"] == "improving"
    assert len(summary["history"]) == 2


def test_credit_summary_trend_declining(ctx):
    fe = ctx.open_finance()
    try:
        fe.save_credit_score(750, {}, "synthetic first score", [])
        fe.save_credit_score(700, {}, "synthetic second score", [])
        summary = _credit_summary(fe)
    finally:
        fe.close()
    assert summary["trend"] == "declining"


# ---------------------------------------------------------------------------
# Loans
# ---------------------------------------------------------------------------

def test_loan_summary_matches_seeded_rows(ctx):
    fe = ctx.open_finance()
    try:
        a1 = fe.create_loan_application("personal", "india", 100_000, 24, None,
                                        {**_MIN_DECISION, "recommended_amount": 100_000})
        fe.update_loan_application_status(a1, "approved")
        a2 = fe.create_loan_application("home", "india", 2_000_000, 60, None,
                                        {**_MIN_DECISION, "recommended_amount": 2_000_000})
        fe.update_loan_application_status(a2, "approved")
        a3 = fe.create_loan_application("personal", "uae", 50_000, 12, None,
                                        {**_MIN_DECISION, "recommended_amount": 50_000})
        fe.update_loan_application_status(a3, "rejected")
        fe.create_loan_application("auto", "india", 300_000, 36, None,
                                   {**_MIN_DECISION, "recommended_amount": 300_000})   # stays pending
        summary = _loan_summary(fe, ctx.store)
    finally:
        fe.close()
    assert summary["total_applications"] == 4
    assert summary["status_counts"] == {"approved": 2, "rejected": 1, "pending": 1}
    assert summary["approval_rate"] == round(2 / 3, 4)
    assert summary["total_approved_amount"] == 2_100_000
    assert summary["by_loan_type"] == {"personal": 2, "home": 1, "auto": 1}


# ---------------------------------------------------------------------------
# Executive summary matches the four individual summaries
# ---------------------------------------------------------------------------

def test_executive_summary_matches_individual_summaries(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        _seed_fraud(fe, aid)
        fe.create_aml_case(aid, "structuring", "HIGH", 60, ["t1"], [], "synthetic")
        fe.save_credit_score(700, {}, "synthetic", [])
        fe.create_loan_application("personal", "india", 100_000, 24, None,
                                   {**_MIN_DECISION, "recommended_amount": 100_000})
        exec_summary = _executive_summary(fe, ctx.store)
    finally:
        fe.close()
    assert "generated_at" in exec_summary
    fe = ctx.open_finance()
    try:
        assert exec_summary["fraud"] == _fraud_summary(fe)
        assert exec_summary["aml"] == _aml_summary(fe)
        assert exec_summary["credit"] == _credit_summary(fe)
        assert exec_summary["loans"] == _loan_summary(fe, ctx.store)
    finally:
        fe.close()


# ---------------------------------------------------------------------------
# Explainability dispatcher reuses the real explain_* tools
# ---------------------------------------------------------------------------

def test_explain_dispatcher_reuses_real_tools(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        fraud_ids = _seed_fraud(fe, aid)
        case_id = fe.create_aml_case(aid, "structuring", "HIGH", 60, ["t1"], [], "synthetic")
        fe.save_credit_score(700, {}, "synthetic", [])
        loan_id = fe.create_loan_application(
            "personal", "india", 100_000, 24, None,
            {**_MIN_DECISION, "recommended_amount": 100_000,
            "explanation": {"credit_score_used": 700, "risk_factors": ["synthetic risk factor"]}})
        fe.update_loan_application_status(loan_id, "rejected")
    finally:
        fe.close()

    fraud_out = _explain(ctx, "fraud", fraud_ids["HIGH"])
    assert fraud_out["available"] is True
    assert fraud_out["fraud_risk_level"] == "HIGH"

    aml_out = _explain(ctx, "aml", case_id)
    assert aml_out["available"] is True

    credit_out = _explain(ctx, "credit", "")
    assert credit_out["available"] is True

    loan_out = _explain(ctx, "loan", loan_id)
    assert loan_out["available"] is True
    assert loan_out["risk_factors"] == ["synthetic risk factor"]

    with pytest.raises(ValueError):
        _explain(ctx, "not-a-real-type", "x")


def test_explain_loan_honest_false_when_not_rejected(ctx):
    fe = ctx.open_finance()
    try:
        loan_id = fe.create_loan_application(
            "personal", "india", 100_000, 24, None,
            {**_MIN_DECISION, "recommended_amount": 100_000})
        # stays 'pending' — never rejected
    finally:
        fe.close()
    out = _explain(ctx, "loan", loan_id)
    assert out["available"] is False
