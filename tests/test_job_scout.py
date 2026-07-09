"""CAREER AUTOPILOT Part 4 — JobScoutSensor + match scoring. All external
MCP/LLM calls are mocked — no live network calls in tests.
"""
import datetime as _dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.automation.closers import _career_briefing_lines
from amy.automation.jobs import HANDLERS
from amy.autonomous import GoalEngine
from amy.career_scout import JobScoutSensor
from amy.collab import CollabDB


@pytest.fixture()
def ctx(tmp_path):
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-scout", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _fake_row(name="jobspy"):
    return SimpleNamespace(name=name, server_url="https://example.invalid/mcp",
                           auth_type="none", auth_ref=None, auth_extra=None,
                           default_target="")


class _FakeClient:
    def __init__(self, result):
        self._result = result

    async def list_tools(self):
        return [{"name": "search_jobs", "description": "", "input_schema": {"properties": {}}}]

    async def call_tool(self, name, arguments=None):
        return self._result


def _mock_jobspy(monkeypatch, jobs):
    row = _fake_row()
    client = _FakeClient({"is_error": False, "text": "", "structured": jobs})
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, source: row if source == "jobspy" else None)
    monkeypatch.setattr("amy.connectors.mcp_call.mcp_client_for", lambda r: client)


class StubLLM:
    def __init__(self, response: dict):
        self._response = response

    def generate(self, system, prompt, context="", sensitive=False, fast=False):
        return (json.dumps(self._response), "scripted")


_JOBS = [{"title": "GenAI Engineer", "company": "Acme", "job_url": "https://example.invalid/1",
         "location": "Bangalore", "description": "LangChain RAG"},
        {"title": "ML Engineer", "company": "Widget Co", "job_url": "https://example.invalid/2",
         "location": "Bangalore", "description": "PyTorch training"}]


def _make_active_career_goal(ctx, target_role="GenAI Engineer"):
    gid = GoalEngine(ctx.collab).create_goal("Become a GenAI Engineer", domain="career")
    ctx.collab.conn.execute(
        "UPDATE goals SET career_meta=? WHERE id=?",
        (json.dumps({"target_role": target_role}), gid))
    ctx.collab.conn.commit()
    ctx.store.set_career_profile(ctx.user_id, target_role=target_role)
    return gid


# ---------------------------------------------------------------------------
# Discovery + dedup
# ---------------------------------------------------------------------------

def test_job_scout_noop_without_active_career_goal(ctx, monkeypatch):
    _mock_jobspy(monkeypatch, _JOBS)
    emitted = JobScoutSensor(ctx.events(), ctx).poll()
    assert emitted == []
    assert ctx.store.list_postings(ctx.user_id) == []


def test_job_scout_discovers_and_dedups(ctx, monkeypatch):
    _make_active_career_goal(ctx)
    _mock_jobspy(monkeypatch, _JOBS)
    # No scoring under test here — force the fast/deterministic no-LLM path
    # rather than letting _get_llm build a real router (slow, network-dependent).
    monkeypatch.setattr("amy.agents.reactive._get_llm", lambda ctx: None)

    first = JobScoutSensor(ctx.events(), ctx).poll()
    assert len(first) == 2
    assert len(ctx.store.list_postings(ctx.user_id)) == 2

    evs = ctx.collab.conn.execute(
        "SELECT type FROM events WHERE type='career.job_discovered'").fetchall()
    assert len(evs) == 2

    second = JobScoutSensor(ctx.events(), ctx).poll()
    assert second == []   # same URLs -> deduped, nothing new
    assert len(ctx.store.list_postings(ctx.user_id)) == 2


# ---------------------------------------------------------------------------
# Match scoring
# ---------------------------------------------------------------------------

def test_job_scout_scores_and_notifies_above_threshold(ctx, monkeypatch):
    _make_active_career_goal(ctx)
    _mock_jobspy(monkeypatch, _JOBS)
    ctx.llm = StubLLM({"scores": [
        {"index": 0, "score": 85, "factors": {"skill_overlap": "high"}},
        {"index": 1, "score": 40, "factors": {"skill_overlap": "low"}},
    ]})

    JobScoutSensor(ctx.events(), ctx).poll()

    postings = {p["title"]: p for p in ctx.store.list_postings(ctx.user_id)}
    assert postings["GenAI Engineer"]["match_score"] == 85
    assert postings["ML Engineer"]["match_score"] == 40

    notifs = ctx.collab.conn.execute(
        "SELECT title FROM notifications WHERE type='career_job_match'").fetchall()
    assert len(notifs) == 1
    assert "GenAI Engineer" in notifs[0]["title"]


def test_job_scout_llm_unavailable_degrades_to_unscored(ctx, monkeypatch):
    _make_active_career_goal(ctx)
    _mock_jobspy(monkeypatch, _JOBS)
    # Force the "no LLM reachable" path deterministically — ctx.llm=None alone
    # would make _get_llm build a REAL LLMRouter and hit real providers/timeouts.
    monkeypatch.setattr("amy.agents.reactive._get_llm", lambda ctx: None)

    emitted = JobScoutSensor(ctx.events(), ctx).poll()
    assert len(emitted) == 2   # postings still discovered even though unscored
    for p in ctx.store.list_postings(ctx.user_id):
        assert p["match_score"] is None
    notifs = ctx.collab.conn.execute(
        "SELECT COUNT(*) n FROM notifications WHERE type='career_job_match'").fetchone()
    assert notifs["n"] == 0


# ---------------------------------------------------------------------------
# Kill switch + job wiring
# ---------------------------------------------------------------------------

def test_job_scout_kill_switch(ctx, monkeypatch):
    monkeypatch.setenv("AMY_AGENT_JOB_SCOUT", "0")
    out = HANDLERS["job_scout_poll"](ctx)
    assert out.get("skipped")


def test_job_scout_job_runs_when_enabled(ctx, monkeypatch):
    monkeypatch.setenv("AMY_AGENT_JOB_SCOUT", "1")
    _make_active_career_goal(ctx)
    _mock_jobspy(monkeypatch, _JOBS)
    monkeypatch.setattr("amy.agents.reactive._get_llm", lambda ctx: None)
    out = HANDLERS["job_scout_poll"](ctx)
    assert out["discovered"] == 2


# ---------------------------------------------------------------------------
# Morning briefing integration
# ---------------------------------------------------------------------------

def test_career_briefing_lines_surface_high_match_jobs(ctx):
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    pid, _ = ctx.store.add_posting_if_new(ctx.user_id, {
        "title": "GenAI Engineer", "company": "Acme", "url": "https://example.invalid/1"})
    ctx.store.set_posting_match(ctx.user_id, pid, 88, {"skill_overlap": "high"})

    lines = _career_briefing_lines(ctx)
    assert lines
    assert "GenAI Engineer" in lines[0]
    assert "88" in lines[0]


def test_career_briefing_lines_silent_below_threshold(ctx):
    pid, _ = ctx.store.add_posting_if_new(ctx.user_id, {
        "title": "Junior Dev", "company": "Acme", "url": "https://example.invalid/2"})
    ctx.store.set_posting_match(ctx.user_id, pid, 40, {})
    assert _career_briefing_lines(ctx) == []


def test_career_briefing_lines_include_application_updates(ctx):
    from amy.events.store import CAREER_APPLICATION_STATUS_CHANGED
    ctx.events().emit(CAREER_APPLICATION_STATUS_CHANGED,
                      {"application_id": "app-1", "status": "interview"}, source="test")
    lines = _career_briefing_lines(ctx)
    assert any("Application updates" in l and "interview" in l for l in lines)


def test_career_briefing_lines_include_unread_stall_nudge(ctx):
    ns = ctx.notify_store()
    ns.create(type="career_stall", title="No recent progress: Become a GenAI Engineer",
              body="stalled", priority="normal", related_entity={})
    lines = _career_briefing_lines(ctx)
    assert any("Stalled" in l for l in lines)


def test_career_briefing_lines_include_next_milestone(ctx):
    from amy.autonomous import GoalEngine
    gid = GoalEngine(ctx.collab).create_goal("Become a GenAI Engineer", domain="career")
    ctx.collab.conn.execute(
        "INSERT INTO milestones(id,goal_id,title,done,position) VALUES(?,?,?,0,?)",
        ("m1", gid, "Week 1: Skill building", 0))
    ctx.collab.conn.commit()
    lines = _career_briefing_lines(ctx)
    assert any("Next milestone: Week 1" in l for l in lines)


def test_job_scout_passes_country_for_home_jurisdiction(ctx, monkeypatch):
    """jobspy quirk: country_indeed must match the location's country or
    indeed silently returns zero results — the scout derives it from the
    home jurisdiction pack (default 'india' -> 'India'), found live when a
    Bangalore profile searched against the tool's USA default."""
    _make_active_career_goal(ctx)
    monkeypatch.setattr("amy.agents.reactive._get_llm", lambda ctx: None)

    captured = {}

    class _CapturingClient:
        async def list_tools(self):
            return [{"name": "search_jobs", "description": "",
                     "input_schema": {"properties": {}}}]

        async def call_tool(self, name, arguments=None):
            captured.update(arguments or {})
            return {"is_error": False, "text": "", "structured": []}

    row = _fake_row()
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, source: row if source == "jobspy" else None)
    monkeypatch.setattr("amy.connectors.mcp_call.mcp_client_for",
                        lambda r: _CapturingClient())

    JobScoutSensor(ctx.events(), ctx).poll()
    assert captured.get("country_indeed") == "India"
