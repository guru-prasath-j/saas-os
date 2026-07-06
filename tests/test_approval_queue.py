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
