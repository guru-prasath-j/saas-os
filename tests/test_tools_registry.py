"""Phase R1 — tool registry: catalog, schema validation, risk gating hook."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy import tools
from amy.tools import registry
from amy.automation.jobs import build_ctx
from amy.collab import CollabDB


@pytest.fixture()
def ctx(tmp_path):
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-test", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def test_catalog_has_tools_with_risks():
    cat = tools.list_tools()
    names = {t["name"] for t in cat}
    assert {"finance_overview", "list_transactions", "set_budget",
            "add_transaction", "delete_transaction"} <= names
    by_name = {t["name"]: t for t in cat}
    assert by_name["finance_overview"]["risk"] == "read"
    assert by_name["set_budget"]["risk"] == "write"
    assert by_name["delete_transaction"]["risk"] == "destructive"
    # every tool has an object schema
    for t in cat:
        assert t["params"].get("type") == "object"


def test_unknown_tool_and_bad_args(ctx):
    with pytest.raises(tools.ToolError):
        tools.invoke(ctx, "no_such_tool", {})
    with pytest.raises(tools.ToolError):  # missing required
        tools.invoke(ctx, "set_budget", {"category": "Food"})
    with pytest.raises(tools.ToolError):  # unexpected param
        tools.invoke(ctx, "list_budgets", {"bogus": 1})
    with pytest.raises(tools.ToolError):  # wrong type
        tools.invoke(ctx, "set_budget", {"category": "Food",
                                         "monthly_limit": "not-a-number"})


def test_numeric_string_coercion(ctx):
    out = tools.invoke(ctx, "set_budget",
                       {"category": "Food", "monthly_limit": "4000"})
    assert out["monthly_limit"] == 4000.0


def test_read_tool_executes(ctx):
    tools.invoke(ctx, "set_budget", {"category": "Food", "monthly_limit": 100})
    budgets = tools.invoke(ctx, "list_budgets", {})
    assert any(b["category"] == "Food" for b in budgets)


def test_agent_gate_hook_intercepts_agent_writes(ctx):
    """actor='agent' + write risk routes through AGENT_GATE when installed;
    read tools never do; actor='human' bypasses the gate."""
    calls = []

    def gate(gctx, tool, args):
        calls.append((tool.name, tool.risk, args))
        return {"gated": True}

    old = registry.AGENT_GATE
    registry.AGENT_GATE = gate
    try:
        out = tools.invoke(ctx, "set_budget",
                           {"category": "X", "monthly_limit": 1}, actor="agent")
        assert out == {"gated": True}
        assert calls and calls[0][0] == "set_budget"

        tools.invoke(ctx, "list_budgets", {}, actor="agent")   # read: no gate
        assert len(calls) == 1

        tools.invoke(ctx, "set_budget",
                     {"category": "Y", "monthly_limit": 2}, actor="human")
        assert len(calls) == 1                                  # human: no gate
    finally:
        registry.AGENT_GATE = old


def test_approve_action_is_human_only(ctx):
    """Handler-level guard: even with no gate installed, an agent actor can
    never reach the approve handler."""
    old = registry.AGENT_GATE
    registry.AGENT_GATE = None
    try:
        with pytest.raises(Exception, match="human-only"):
            tools.invoke(ctx, "approve_action", {"approval_id": "x"}, actor="agent")
    finally:
        registry.AGENT_GATE = old


def test_agent_kill_switch_env(monkeypatch):
    from amy.config import agent_enabled
    assert agent_enabled("budget") is True                       # default on
    assert agent_enabled("payer", destructive_default_off=True) is False
    monkeypatch.setenv("AMY_AGENT_BUDGET", "0")
    assert agent_enabled("budget") is False
    monkeypatch.setenv("AMY_AGENT_PAYER", "1")
    assert agent_enabled("payer", destructive_default_off=True) is True
