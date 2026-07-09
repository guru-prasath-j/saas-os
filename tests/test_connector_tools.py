"""CONNECTOR COMPLETION Part 1 — GitHub/Plane registry tools: external-pin
tier enforcement, connector_calls ledger, and the resolve/call/log helper.
All external MCP calls are mocked — no live network calls in tests.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy import tools
from amy.automation import build_ctx
from amy.collab import CollabDB


@pytest.fixture()
def ctx(tmp_path):
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-conn", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _fake_row(name="github", default_target="acme/widgets"):
    return SimpleNamespace(name=name, server_url="https://example.invalid/mcp",
                           auth_type="none", auth_ref=None, auth_extra=None,
                           default_target=default_target)


class _FakeClient:
    def __init__(self, tool_names, result):
        self._tool_names = tool_names
        self._result = result

    async def list_tools(self):
        return [{"name": n, "description": "", "input_schema": {"properties": {}}}
                for n in self._tool_names]

    async def call_tool(self, name, arguments=None):
        return self._result


def _agent_invoke(ctx, name, args, agent="test_agent", reasoning="because test"):
    ctx._extras["agent_name"] = agent
    ctx._extras["agent_reasoning"] = reasoning
    return tools.invoke(ctx, name, args, actor="agent")


def test_plane_create_task_parks_tier2_even_with_write_tier_0(ctx, monkeypatch):
    """External-pin: an agent-proposed plane_create_task must park at tier 2
    even when AMY_AGENT_WRITE_TIER=0 would otherwise auto-execute an
    ordinary internal write."""
    monkeypatch.setenv("AMY_AGENT_WRITE_TIER", "0")
    out = _agent_invoke(ctx, "plane_create_task", {"title": "Fix the thing"})
    assert out["status"] == "pending", (
        f"external tool must park regardless of AMY_AGENT_WRITE_TIER, got {out}")
    ap = ctx.store.get_approval(out["approval_id"])
    assert ap["tier"] == 2
    assert ap["risk"] == "write"


def test_github_comment_parks_tier2_even_with_write_tier_0(ctx, monkeypatch):
    monkeypatch.setenv("AMY_AGENT_WRITE_TIER", "0")
    out = _agent_invoke(ctx, "github_comment",
                        {"number": 42, "body": "looks good"})
    assert out["status"] == "pending"
    assert ctx.store.get_approval(out["approval_id"])["tier"] == 2


def test_ordinary_write_tool_still_honors_write_tier_0(ctx, monkeypatch):
    """Sanity check that the external hard-pin is specific to external tools
    — an ordinary (non-external) write tool still obeys AMY_AGENT_WRITE_TIER."""
    monkeypatch.setenv("AMY_AGENT_WRITE_TIER", "0")
    out = _agent_invoke(ctx, "set_budget", {"category": "Food", "monthly_limit": 5000})
    assert out["status"] == "auto_executed"


def test_human_actor_plane_create_task_executes_and_logs_connector_call(ctx, monkeypatch):
    """A human-actor call bypasses the approval gate and calls through to
    the MCP connector immediately, logging the attempt to connector_calls."""
    row = _fake_row(name="plane", default_target="proj-123")
    client = _FakeClient(["create_work_item"],
                         {"is_error": False, "text": "", "structured": {"id": "wi-1"}})
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, source: row)
    monkeypatch.setattr("amy.connectors.mcp_call.mcp_client_for",
                        lambda r: client)

    out = tools.invoke(ctx, "plane_create_task", {"title": "Ship it"}, actor="human")
    assert out["result"]["structured"] == {"id": "wi-1"}
    assert out["result"]["is_error"] is False

    calls = ctx.store.recent_connector_calls(connector="plane")
    assert len(calls) == 1
    assert calls[0]["ok"] == 1
    assert calls[0]["tool"] == "create_work_item"


def test_github_list_prs_read_tool_uses_default_target(ctx, monkeypatch):
    row = _fake_row(name="github", default_target="acme/widgets")
    seen_args = {}

    class _Client(_FakeClient):
        async def call_tool(self, name, arguments=None):
            seen_args.update(arguments or {})
            return self._result

    client = _Client(["list_pull_requests"],
                     {"is_error": False, "text": "", "structured": [{"number": 1}]})
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, source: row)
    monkeypatch.setattr("amy.connectors.mcp_call.mcp_client_for",
                        lambda r: client)

    out = tools.invoke(ctx, "github_list_prs", {}, actor="human")
    assert out["result"]["structured"] == [{"number": 1}]
    assert seen_args["owner"] == "acme" and seen_args["repo"] == "widgets"


def test_no_connector_registered_raises_clear_error(ctx, monkeypatch):
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, source: None)
    with pytest.raises(Exception, match="no 'github' MCP connector"):
        tools.invoke(ctx, "github_list_issues", {}, actor="human")
