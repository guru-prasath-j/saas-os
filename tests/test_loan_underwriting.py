"""Loan Underwriting Module (Phase 5) — illustrative underwriting
simulator, never a real lending decision.

All accounts/transactions/applicants constructed in this file are
SYNTHETIC test fixtures, not real financial data. See
amy/finance/loan_engine.py's module docstring for the "illustrative
simulator" framing and the reuse decisions (afford.py's can_afford()
as-is, a locally-restated EMI/Loan-category debt-to-income signal).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.automation.executors import approve, reject
from amy.collab import CollabDB
from amy.finance import loan_engine


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-loan", "loan@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _seed_healthy_income(fe, aid):
    fe.add_income_source("Salary", "salary", 80000, "monthly")
    for i in range(1, 7):
        fe.add_transaction(80000, "Income", "Salary", date=f"2026-0{i}-01", account_id=aid)
        fe.add_transaction(-20000, "Food", "Groceries", date=f"2026-0{i}-15", account_id=aid)


# ---------------------------------------------------------------------------
# Jurisdiction limits are actually applied
# ---------------------------------------------------------------------------

def test_jurisdiction_cap_applied_not_silently_approved(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        _seed_healthy_income(fe, aid)
        # india personal loan_limits cap is 2,000,000 INR (Phase 4) — request well above it
        decision = loan_engine.underwrite(fe, ctx.collab, "personal", "india",
                                          5_000_000, 36)
    finally:
        fe.close()
    limits = decision["explanation"]["jurisdiction_limits_applied"]
    assert limits["was_capped"] is True
    assert decision["recommended_amount"] == limits["loan_limit"] == 2_000_000
    assert decision["recommended_amount"] < 5_000_000


def test_within_limit_request_is_not_capped(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        _seed_healthy_income(fe, aid)
        decision = loan_engine.underwrite(fe, ctx.collab, "personal", "india", 100_000, 24)
    finally:
        fe.close()
    assert decision["explanation"]["jurisdiction_limits_applied"]["was_capped"] is False
    assert decision["recommended_amount"] == 100_000


# ---------------------------------------------------------------------------
# Missing credit score doesn't crash — it's an honest input, not fabricated
# ---------------------------------------------------------------------------

def test_missing_credit_score_still_produces_a_decision(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        _seed_healthy_income(fe, aid)
        assert fe.get_latest_credit_score() is None   # no Phase 3 score computed
        decision = loan_engine.underwrite(fe, ctx.collab, "personal", "india", 100_000, 24)
    finally:
        fe.close()
    assert decision["explanation"]["credit_score_used"] is None
    assert 0.0 <= decision["approval_probability"] <= 1.0
    assert decision["risk_category"] in ("LOW", "MEDIUM", "HIGH")
    assert any("No Amy Score" in f for f in decision["explanation"]["risk_factors"])


# ---------------------------------------------------------------------------
# apply_for_loan() always parks a fixed tier-2 approval
# ---------------------------------------------------------------------------

def test_apply_for_loan_always_creates_pending_tier2_approval(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        # deliberately thin profile -> likely HIGH risk_category
        fe.add_transaction(-500, "Food", "Snack", date="2026-01-01", account_id=aid)
    finally:
        fe.close()

    result = loan_engine.apply_for_loan(ctx, "personal", "india", 100_000, 24)
    assert result["approval"]["status"] == "pending"
    approval_id = result["approval"]["approval_id"]

    pending = ctx.store.list_approvals("pending")
    assert len(pending) == 1
    assert pending[0]["tier"] == 2
    assert pending[0]["payload"]["application_id"] == result["application_id"]

    app = fe = ctx.open_finance()
    try:
        app = fe.get_loan_application(result["application_id"])
    finally:
        fe.close()
    assert app["status"] == "pending"
    assert app["approval_id"] == approval_id


# ---------------------------------------------------------------------------
# Approval generates a real amortization schedule
# ---------------------------------------------------------------------------

def test_approving_generates_reducing_balance_schedule_summing_to_principal(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        _seed_healthy_income(fe, aid)
    finally:
        fe.close()

    result = loan_engine.apply_for_loan(ctx, "personal", "india", 100_000, 12)
    approval_id = result["approval"]["approval_id"]
    application_id = result["application_id"]

    out = approve(ctx, approval_id)
    assert out["status"] == "executed"

    fe = ctx.open_finance()
    try:
        app = fe.get_loan_application(application_id)
        schedule = fe.get_loan_schedule(application_id)
    finally:
        fe.close()
    assert app["status"] == "approved"
    assert len(schedule) == 12
    assert schedule[-1]["balance"] == 0.0
    total_principal = round(sum(r["principal"] for r in schedule), 2)
    assert total_principal == round(result["decision"]["recommended_amount"], 2)


# ---------------------------------------------------------------------------
# Rejection via the STANDARD Approval Inbox path (no loan-specific endpoint)
# ---------------------------------------------------------------------------

def test_rejection_via_standard_executor_reflected_by_reconciliation(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        _seed_healthy_income(fe, aid)
    finally:
        fe.close()

    result = loan_engine.apply_for_loan(ctx, "personal", "india", 100_000, 12)
    approval_id = result["approval"]["approval_id"]
    application_id = result["application_id"]

    reject(ctx, approval_id, "synthetic test rejection")

    fe = ctx.open_finance()
    try:
        # the row itself is untouched until something reads it...
        raw = fe.get_loan_application(application_id)
        assert raw["status"] == "pending"
        # ...reconciliation on read is what catches it up
        reconciled = loan_engine.get_application(fe, ctx.store, application_id)
        assert reconciled["status"] == "rejected"
        # and it's now persisted, not just returned in-memory
        assert fe.get_loan_application(application_id)["status"] == "rejected"
    finally:
        fe.close()


# ---------------------------------------------------------------------------
# Islamic financing — jurisdiction-gated
# ---------------------------------------------------------------------------

def test_islamic_financing_allowed_in_uae_rejected_in_india(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        _seed_healthy_income(fe, aid)

        uae_decision = loan_engine.underwrite(fe, ctx.collab, "personal", "uae",
                                              50_000, 24, financing_structure="murabaha")
        assert uae_decision["financing_structure"] == "murabaha"
        assert uae_decision["interest_calculation_method"] == "islamic"

        with pytest.raises(ValueError, match="Islamic financing"):
            loan_engine.underwrite(fe, ctx.collab, "personal", "india",
                                   50_000, 24, financing_structure="murabaha")
    finally:
        fe.close()


def test_qard_hasan_is_profit_free(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        _seed_healthy_income(fe, aid)
        decision = loan_engine.underwrite(fe, ctx.collab, "personal", "uae",
                                          60_000, 12, financing_structure="qard_hasan")
    finally:
        fe.close()
    # profit-free: total repayment across the schedule equals the principal exactly
    schedule = loan_engine.build_schedule(
        decision["recommended_amount"], decision["recommended_rate"],
        decision["term_months"], "islamic", structure="qard_hasan")
    assert sum(r["interest"] for r in schedule) == 0.0


# ---------------------------------------------------------------------------
# Assistant tools read STORED data only
# ---------------------------------------------------------------------------

def test_explain_simulate_compare_explain_emi_read_stored_data(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        _seed_healthy_income(fe, aid)
    finally:
        fe.close()

    result = loan_engine.apply_for_loan(ctx, "home", "india", 5_000_000, 60)
    application_id = result["application_id"]
    approval_id = result["approval"]["approval_id"]

    # not rejected yet -> honest "not rejected"
    fe = ctx.open_finance()
    try:
        before = loan_engine.explain_loan_rejection(fe, ctx.store, application_id)
    finally:
        fe.close()
    assert before["available"] is False

    reject(ctx, approval_id, "synthetic")
    fe = ctx.open_finance()
    try:
        after = loan_engine.explain_loan_rejection(fe, ctx.store, application_id)
    finally:
        fe.close()
    assert after["available"] is True
    assert after["credit_score_used"] is None
    assert isinstance(after["risk_factors"], list)

    fe = ctx.open_finance()
    try:
        refi = loan_engine.simulate_refinancing(fe, application_id, 0.05)
        compare = loan_engine.compare_loan_offers(fe, ctx.store, [application_id, "does-not-exist"])
        emi_info = loan_engine.explain_emi(fe, application_id)
    finally:
        fe.close()
    assert refi["available"] is True
    assert refi["simulated_rate"] == 0.05
    assert compare["offers"][0]["available"] is True
    assert compare["offers"][1]["available"] is False
    assert emi_info["available"] is True
    assert emi_info["schedule_generated"] is False   # rejected, never approved -> no schedule
