"""Phase R5 — jurisdiction-aware, locale-rendered morning briefing."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.automation.closers import morning_briefing
from amy.collab import CollabDB


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-brief", "t@example.com", cdb, tmp_path, llm_router=None,
                  jurisdictions=["india", "uae"], language="en-IN")
    yield c
    cdb.close()


def test_briefing_multi_jurisdiction(ctx):
    fe = ctx.open_finance()
    try:
        # Indian account (home) + a UAE account in AED
        aid_in = fe.add_account("HDFC", "HDFC", account_type="savings")
        fe.add_transaction(150000, "Income", "SALARY", account_id=aid_in)
        aid_ae = fe.add_account("Emirates", "ENBD", account_type="savings")
        fe.update_account(aid_ae, jurisdiction="uae", currency="AED")
        fe.add_transaction(5000, "Income", "SALARY AE", account_id=aid_ae)
        # custodial money must not appear anywhere
        cid = fe.add_account("Trust", "SBI", account_type="custodial")
        fe.add_transaction(999999, "Income", "REFILL", account_id=cid)
        # an obligation needing an estimate
        from amy.obligations import activate
        activate(fe, "india", "advance_tax")
    finally:
        fe.close()
    # an agent insight from "yesterday's" activity
    ctx.events().emit("agent.insight", {"agent": "budget",
                                        "summary": "Food at 95% of budget",
                                        "reasoning": "r"}, source="budget_agent")

    out = morning_briefing(ctx)
    body = out["summary"]

    assert out["created"] is True
    assert "₹" in body                               # home-pack locale rendering
    assert "999,999" not in body and "9,99,999" not in body   # custodial excluded
    assert "india:" in body and "uae:" in body       # per-jurisdiction breakdown
    assert "Deadlines:" in body                      # cross-jurisdiction calendar
    assert "in india" in body                        # e.g. advance-tax installment
    assert "in uae" in body                          # e.g. VAT filing
    assert "set an annual estimate" in body          # obligation status line
    assert "Agent insights: Food at 95% of budget" in body
    assert "No approvals waiting." in body

    # digest event emitted + journaled type
    evs = ctx.collab.conn.execute(
        "SELECT type FROM events WHERE type='digest.generated'").fetchall()
    assert evs

    # dedup: second run the same day creates nothing new
    out2 = morning_briefing(ctx)
    assert out2["created"] is False


def test_briefing_survives_empty_state(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    try:
        c = build_ctx("u-empty", "t@example.com", cdb, tmp_path)
        out = morning_briefing(c)
        assert out["created"] is True and out["summary"]
    finally:
        cdb.close()
