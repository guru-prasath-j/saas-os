"""CONNECTOR COMPLETION Part 2 — GitHubSensor/PlaneSensor diff cycles,
pr_to_task dedup, and meeting_prep note-writing. All external MCP/Google
calls are mocked — no live network calls in tests.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.collab import CollabDB
from amy.events.factory import get_events


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    ctx = build_ctx("u-conn2", "t@example.com", cdb, tmp_path, llm_router=None)
    es = get_events("u-conn2", cdb, index_dir=tmp_path, user_email="t@example.com", ctx=ctx)
    yield ctx, es, tmp_path
    cdb.close()


def _fake_row(name="github", default_target="acme/widgets"):
    return SimpleNamespace(name=name, server_url="https://example.invalid/mcp",
                           auth_type="none", auth_ref=None, auth_extra=None,
                           default_target=default_target)


class _FakeClient:
    def __init__(self, tool_names, results_by_tool):
        self._tool_names = tool_names
        self._results = results_by_tool

    async def list_tools(self):
        return [{"name": n, "description": "", "input_schema": {"properties": {}}}
                for n in self._tool_names]

    async def call_tool(self, name, arguments=None):
        return self._results[name]


def _install_github_mocks(monkeypatch, prs, issues):
    row = _fake_row(name="github", default_target="acme/widgets")
    client = _FakeClient(
        ["list_pull_requests", "list_issues"],
        {"list_pull_requests": {"is_error": False, "text": "", "structured": prs},
         "list_issues": {"is_error": False, "text": "", "structured": issues}})
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, source: row if source == "github" else None)
    monkeypatch.setattr("amy.connectors.mcp_call.mcp_client_for", lambda r: client)


def test_github_sensor_diff_cycle_first_emits_second_is_quiet(env, monkeypatch):
    from amy.connectors.sensors import GitHubSensor

    pr = {"number": 7, "title": "Add feature", "html_url": "https://x/7",
          "requested_reviewers": ["alice"], "state": "open"}
    _install_github_mocks(monkeypatch, prs=[pr], issues=[])

    ctx, es, tmp = env
    sensor = GitHubSensor(es, ctx)

    first = sensor.poll()
    assert any(e.get("number") == 7 for e in first), "first poll should emit the new PR"
    events_seen = es.recent("github.pr_review_requested")
    assert len(events_seen) == 1

    second = sensor.poll()
    assert second == [], "identical second poll must emit nothing"
    assert len(es.recent("github.pr_review_requested")) == 1, "no duplicate event row"


def test_github_sensor_pr_status_changed_only_fires_on_transition(env, monkeypatch):
    from amy.connectors.sensors import GitHubSensor

    pr = {"number": 9, "title": "Fix bug", "html_url": "https://x/9",
          "requested_reviewers": [], "state": "open"}
    _install_github_mocks(monkeypatch, prs=[pr], issues=[])
    ctx, es, tmp = env
    sensor = GitHubSensor(es, ctx)

    first = sensor.poll()
    assert first == [], "first sighting sets a baseline state, doesn't fire 'changed'"

    pr["state"] = "changes_requested"
    _install_github_mocks(monkeypatch, prs=[pr], issues=[])
    second = sensor.poll()
    assert any(e.get("state") == "changes_requested" for e in second)
    assert len(es.recent("github.pr_status_changed")) == 1

    # unchanged state on the next poll -> quiet again
    third = sensor.poll()
    assert third == []


def test_pr_to_task_agent_dedups_same_pr_across_two_events(env, monkeypatch):
    """Same PR review-requested event fired twice must produce exactly one
    Plane task approval row — the agent must not double-propose."""
    ctx, es, tmp = env
    # no live LLM calls in tests — the reasoning-summary LLM call is a
    # nice-to-have (see _pr_to_task_agent's try/except around it), skip it
    monkeypatch.setattr("amy.agents.reactive._get_llm", lambda ctx: None)

    calls = {"n": 0}
    row = _fake_row(name="plane", default_target="proj-1")
    client = _FakeClient(["create_work_item"],
                         {"create_work_item": {"is_error": False, "text": "",
                                               "structured": {"id": "wi-1"}}})

    def fake_client_for(r):
        calls["n"] += 1
        return client
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, source: row if source == "plane" else None)
    monkeypatch.setattr("amy.connectors.mcp_call.mcp_client_for", fake_client_for)

    payload = {"repo": "acme/widgets", "number": 11, "title": "Needs eyes",
              "url": "https://x/11"}
    es.emit("github.pr_review_requested", payload, source="test")
    es.emit("github.pr_review_requested", payload, source="test")

    approvals = ctx.store.list_approvals(status=None, limit=50)
    plane_creates = [a for a in approvals if a["action_type"] == "tool_call"
                     and a["payload"].get("tool") == "plane_create_task"]
    assert len(plane_creates) == 1, (
        f"expected exactly 1 plane_create_task approval, got {len(plane_creates)}")


def test_pr_to_task_disabled_by_kill_switch(env, monkeypatch):
    monkeypatch.setenv("AMY_AGENT_PR_TASK", "0")
    ctx, cdb, tmp = env[0], None, env[2]
    from amy.collab import CollabDB
    cdb2 = CollabDB(str(tmp / "collab.db"))
    try:
        es2 = get_events("u-conn2", cdb2, index_dir=tmp, user_email="t@example.com")
        es2.emit("github.pr_review_requested",
                {"repo": "acme/widgets", "number": 99, "title": "x", "url": "https://x/99"},
                source="test")
        approvals = es2.recent("agent.insight")
        assert not any(a["payload"].get("agent") == "pr_to_task" for a in approvals)
    finally:
        cdb2.close()


def test_meeting_prep_writes_vault_note_and_insight(env, monkeypatch):
    from amy.agents.reactive import meeting_prep_check
    import datetime as _dt

    ctx, es, tmp = env
    soon = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=15)).isoformat()

    def fake_invoke(ctx, name, args=None, actor="human"):
        if name == "meet_upcoming_meetings":
            return {"meetings": [{"id": "meet-1", "title": "Widgets sync",
                                  "start": soon, "meet_link": "https://meet/1",
                                  "attendees": []}]}
        if name == "plane_list_tasks":
            return {"truncated": False, "result": {"is_error": False, "text": "",
                    "structured": [{"name": "Widgets rollout"}]}}
        if name == "github_list_prs":
            return {"truncated": False, "result": {"is_error": False, "text": "", "structured": []}}
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr("amy.tools.invoke", fake_invoke)

    n = meeting_prep_check(es, ctx)
    assert n == 1

    insights = es.recent("agent.insight")
    assert any(i["payload"].get("agent") == "meeting_prep" for i in insights)

    from amy.saas import tenancy
    vault = tenancy.resolve_vault_dir("u-conn2")
    notes = list(vault.rglob("*meeting*")) + list(vault.rglob("*Meeting*"))
    assert notes, "expected a meeting-prep vault note to be written"

    # idempotent: calling again for the SAME meeting must not error and
    # must not spam a second insight for a meeting already prepped this run
    n2 = meeting_prep_check(es, ctx)
    assert n2 == 1   # still within window; note write itself is idempotent (dedup on eid)
