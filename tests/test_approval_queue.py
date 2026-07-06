"""Phase R3 — unified approval queue lifecycle:
park (agent-invoked write) → approve → execute → decision recorded; reject;
expiry; destructive tier hard-pinned."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy import tools
from amy.automation import build_ctx, executors
from amy.collab import CollabDB


@pytest.fixture()
def ctx(tmp_path):
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-appr", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _agent_invoke(ctx, name, args, agent="test_agent", reasoning="because test"):
    ctx._extras["agent_name"] = agent
    ctx._extras["agent_reasoning"] = reasoning
    return tools.invoke(ctx, name, args, actor="agent")


def test_agent_write_parks_with_metadata(ctx):
    out = _agent_invoke(ctx, "set_budget",
                        {"category": "Food", "monthly_limit": 5000})
    assert out["status"] == "pending"
    ap = ctx.store.get_approval(out["approval_id"])
    assert ap["action_type"] == "tool_call"
    assert ap["payload"] == {"tool": "set_budget",
                             "args": {"category": "Food", "monthly_limit": 5000.0}}
    assert ap["reasoning"] == "because test"
    assert ap["risk"] == "write"
    assert "category=Food" in ap["affected_entity"]
    assert ap["expires_at"] > dt.datetime.now(dt.timezone.utc).isoformat()
    assert ap["source"] == "test_agent"
    # nothing executed yet
    fe = ctx.open_finance()
    try:
        assert fe.list_budgets() == []
    finally:
        fe.close()


def test_approve_executes_and_records_decision(ctx):
    out = _agent_invoke(ctx, "set_budget",
                        {"category": "Travel", "monthly_limit": 900})
    res = executors.approve(ctx, out["approval_id"])
    assert res["status"] == "executed"
    fe = ctx.open_finance()
    try:
        assert any(b["category"] == "Travel" and b["monthly_limit"] == 900
                   for b in fe.list_budgets())
    finally:
        fe.close()
    rows = ctx.collab.conn.execute(
        "SELECT title FROM decisions ORDER BY ts DESC").fetchall()
    assert rows and rows[0]["title"].startswith("Approved: test_agent: set_budget")


def test_reject_records_decision_and_skips_execution(ctx):
    out = _agent_invoke(ctx, "set_budget",
                        {"category": "Games", "monthly_limit": 100})
    executors.reject(ctx, out["approval_id"], reason="not now")
    ap = ctx.store.get_approval(out["approval_id"])
    assert ap["status"] == "rejected"
    fe = ctx.open_finance()
    try:
        assert not any(b["category"] == "Games" for b in fe.list_budgets())
    finally:
        fe.close()
    rows = ctx.collab.conn.execute(
        "SELECT title FROM decisions ORDER BY ts DESC").fetchall()
    assert rows and rows[0]["title"].startswith("Rejected:")


def test_expiry_sweep(ctx):
    aid = ctx.store.create_approval(
        tier=2, action_type="tool_call", title="stale", payload={},
        expires_at="2000-01-01T00:00:00+00:00")
    pend = ctx.store.list_approvals("pending")   # triggers expire_stale()
    assert all(p["id"] != aid for p in pend)
    assert ctx.store.get_approval(aid)["status"] == "expired"
    with pytest.raises(ValueError, match="expired"):
        executors.approve(ctx, aid)


def test_approve_clears_its_approval_needed_notification(ctx):
    """Bug found via manual testing: acting on an approval left its bell
    notification unread forever - the badge stayed stuck at 1 even after
    the user had already approved/rejected the item."""
    out = _agent_invoke(ctx, "set_budget",
                        {"category": "Food", "monthly_limit": 1800})
    ns = ctx.notify_store()
    unread_before = [n for n in ns.list() if n["type"] == "approval_needed"
                     and n["read_at"] is None]
    assert len(unread_before) == 1
    assert ns.unread_count() == 1

    executors.approve(ctx, out["approval_id"])

    unread_after = [n for n in ns.list() if n["type"] == "approval_needed"
                    and n["read_at"] is None]
    assert unread_after == []
    assert ns.unread_count() == 0


def test_reject_clears_its_approval_needed_notification(ctx):
    out = _agent_invoke(ctx, "set_budget",
                        {"category": "Games", "monthly_limit": 100})
    assert ctx.notify_store().unread_count() == 1

    executors.reject(ctx, out["approval_id"], reason="not now")

    assert ctx.notify_store().unread_count() == 0


def test_other_unread_notifications_are_not_touched(ctx):
    """Clearing one approval's notification must not mark unrelated
    notifications as read."""
    ctx.notify_store().create(type="custodial_nudge", title="unrelated",
                              body="should stay unread", priority="normal")
    out = _agent_invoke(ctx, "set_budget",
                        {"category": "Food", "monthly_limit": 1800})
    assert ctx.notify_store().unread_count() == 2

    executors.approve(ctx, out["approval_id"])

    assert ctx.notify_store().unread_count() == 1
    remaining = [n for n in ctx.notify_store().list() if n["read_at"] is None]
    assert remaining[0]["type"] == "custodial_nudge"


def test_expire_stale_clears_notification_too(ctx):
    out = _agent_invoke(ctx, "set_budget",
                        {"category": "Food", "monthly_limit": 1800})
    assert ctx.notify_store().unread_count() == 1

    # force the approval into the past so the sweep picks it up
    ctx.collab.conn.execute(
        "UPDATE approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
        (out["approval_id"],))
    ctx.collab.conn.commit()

    n = ctx.store.expire_stale()
    assert n == 1
    assert ctx.store.get_approval(out["approval_id"])["status"] == "expired"
    assert ctx.notify_store().unread_count() == 0


def test_destructive_tier_hard_pinned(monkeypatch):
    monkeypatch.setenv("AMY_AGENT_WRITE_TIER", "0")
    assert executors._tier_for("destructive") == 2
    assert executors._tier_for("write") == 0


def test_human_actor_still_direct(ctx):
    out = tools.invoke(ctx, "set_budget",
                       {"category": "Direct", "monthly_limit": 10}, actor="human")
    assert out.get("category") == "Direct"   # executed, not parked
    fe = ctx.open_finance()
    try:
        assert any(b["category"] == "Direct" for b in fe.list_budgets())
    finally:
        fe.close()
