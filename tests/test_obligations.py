"""Phase R7A-2 — obligations engine: one preset per jurisdiction + the
custodial exclusion rail + the agent's queue-only payment proposals."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.collab import CollabDB
from amy.obligations import (activate, all_statuses, compute_status,
                             list_active, qualifying_wealth, update_config)
from amy.obligations.agent import obligation_check


@pytest.fixture()
def ctx(tmp_path):
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-obl", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _seed_wealth(fe, savings=100000.0, custodial=50000.0):
    aid = fe.add_account("Main", "TestBank", account_type="savings")
    fe.add_transaction(savings, "Income", "SALARY", account_id=aid)
    cid = fe.add_account("Trust", "TestBank", account_type="custodial")
    fe.add_transaction(custodial, "Income", "REFILL", account_id=cid)
    return aid, cid


# --- UAE preset: zakat (wealth_rate, hijri year, custodial excluded) -----------

def test_zakat_uae_excludes_custodial(ctx):
    fe = ctx.open_finance()
    try:
        _seed_wealth(fe, savings=100000, custodial=50000)
        oid = activate(fe, "uae", "zakat")
        st = compute_status(fe, list_active(fe)[0], dt.date(2026, 7, 6))
    finally:
        fe.close()
    assert oid
    assert st["kind"] == "wealth_rate"
    assert st["qualifying_wealth"] == 100000        # custodial 50k NOT counted
    assert st["state"] == "accruing"                # above the AED-pack threshold
    assert st["estimated_liability"] == pytest.approx(2500.0)   # 2.5%
    assert st["currency"] == "AED"
    assert st["rules_shown"]["rate"] == 0.025
    assert st["rules_shown"]["calendar_system"] == "hijri"
    assert "ESTIMATES" in st["disclaimer"]


def test_zakat_below_threshold(ctx):
    fe = ctx.open_finance()
    try:
        _seed_wealth(fe, savings=5000)
        activate(fe, "uae", "zakat")
        st = compute_status(fe, list_active(fe)[0], dt.date(2026, 7, 6))
    finally:
        fe.close()
    assert st["state"] == "below_threshold"
    assert st["estimated_liability"] == 0.0


# --- India preset: advance_tax (scheduled_estimate, fiscal Apr–Mar) -------------

def test_advance_tax_india_installments(ctx):
    fe = ctx.open_finance()
    try:
        oid = activate(fe, "india", "advance_tax")
        st0 = compute_status(fe, list_active(fe)[0], dt.date(2026, 7, 6))
        assert st0["state"] == "needs_estimate"     # no estimate configured yet
        update_config(fe, oid, {"estimated_annual_amount": 100000,
                                "paid_to_date": 15000})
        st = compute_status(fe, list_active(fe)[0], dt.date(2026, 7, 6))
    finally:
        fe.close()
    assert st["state"] == "scheduled"
    assert st["next_due"] == "2026-09-15"           # 2nd installment
    assert "2nd installment" in st["next_label"]
    # cumulative 45% of 100k = 45k, minus 15k already paid
    assert st["amount_due_by_next"] == 30000.0
    assert st["currency"] == "INR"


# --- US preset: quarterly_tax_estimate ------------------------------------------

def test_quarterly_estimate_us(ctx):
    fe = ctx.open_finance()
    try:
        oid = activate(fe, "us", "quarterly_tax_estimate",
                       {"estimated_annual_amount": 8000})
        st = compute_status(fe, list_active(fe)[0], dt.date(2026, 7, 1))
    finally:
        fe.close()
    assert st["next_due"] == "2026-09-15"           # Q3
    assert st["amount_due_by_next"] == 6000.0       # 75% of 8000
    assert st["currency"] == "USD"


# --- savings_commitment proves the engine is not a tax/religion engine ----------

def test_savings_commitment_generic(ctx):
    fe = ctx.open_finance()
    try:
        fe.add_income_source("Job", amount=50000, recurrence="monthly")
        activate(fe, "india", "savings_commitment")
        st = compute_status(fe, list_active(fe)[0], dt.date(2026, 7, 6))
    finally:
        fe.close()
    assert st["kind"] == "recurring_commitment"
    assert st["monthly_target"] == 5000.0           # 10% of 50k


# --- the agent: notification + payment proposal parked in the queue ---------------

def test_obligation_agent_proposes_via_queue(ctx, monkeypatch):
    fe = ctx.open_finance()
    try:
        _seed_wealth(fe, savings=200000)
        activate(fe, "india", "advance_tax",
                 {"estimated_annual_amount": 100000})
    finally:
        fe.close()
    # freeze "due soon": Sep 1 → Sep 15 installment is 14 days away
    import amy.obligations.agent as agent_mod

    class _FakeDate(dt.date):
        @classmethod
        def today(cls):
            return dt.date(2026, 9, 1)
    monkeypatch.setattr(agent_mod._dt, "date", _FakeDate)

    out = obligation_check(ctx)
    assert out["notified"] >= 1
    assert out["payment_proposals"] == 1

    pend = ctx.store.list_approvals("pending")
    assert pend and pend[0]["payload"]["tool"] == "add_transaction"
    assert pend[0]["source"] == "obligation_agent"
    assert "estimate" in pend[0]["reasoning"].lower()
    # amount is the installment figure, negative (an expense record)
    assert pend[0]["payload"]["args"]["amount"] == -45000.0
    # nothing recorded until approval
    fe = ctx.open_finance()
    try:
        assert not fe.list_transactions(category="Obligation — Advance tax (installments)")
    finally:
        fe.close()
    # re-run: dedup key prevents duplicate proposals
    out2 = obligation_check(ctx)
    assert out2["payment_proposals"] == 0


def test_kill_switch(ctx, monkeypatch):
    monkeypatch.setenv("AMY_AGENT_OBLIGATION", "0")
    assert obligation_check(ctx) == {"skipped": "AMY_AGENT_OBLIGATION disabled"}
