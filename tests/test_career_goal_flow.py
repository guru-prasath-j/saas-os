"""CAREER AUTOPILOT Part 2 — career goal flow: orchestrator template,
career_goal agent (propose + stall nudge). All external MCP calls degrade
via their own try/except (no mocking needed for the template's happy path);
Plane batch creation only ever PARKS in this test (external -> tier 2), so
it never actually calls the connector.
"""
import datetime as _dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.automation.orchestrator import _is_career_goal, run_goal
from amy.agents.reactive import career_goal_stall_check, register_reactive_agents
from amy.collab import CollabDB


@pytest.fixture()
def ctx(tmp_path):
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-career-goal", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


class StubLLM:
    """Matches the real LLMRouter.generate signature (system, prompt,
    context="", sensitive=False, fast=False) — test_orchestrator.py's
    ScriptedLLM predates the `fast` kwarg and is itself a known pre-existing
    broken test (confirmed via git stash), not a template to copy."""
    def __init__(self, responses):
        import json
        self._json = json
        self.responses = list(responses)

    def generate(self, system, prompt, context="", sensitive=False, fast=False):
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return (self._json.dumps(r), "scripted")


# ---------------------------------------------------------------------------
# _is_career_goal detector
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("become a GenAI engineer in 2 months", True),
    ("I want a career change into data science", True),
    ("cut my spending 10% this quarter", False),
    ("become debt-free by December", False),
    ("switch to a healthier diet", False),
    ("help me switch to backend engineering", True),
])
def test_is_career_goal_detector(text, expected):
    assert _is_career_goal(text) is expected


# ---------------------------------------------------------------------------
# Orchestrator career template
# ---------------------------------------------------------------------------

def test_career_shaped_goal_runs_template(ctx):
    """No LLM configured — every step degrades to its documented fallback,
    proving the template never hard-depends on a provider being up."""
    ctx.llm = None
    out = run_goal(ctx, "Become a GenAI Engineer in 2 months")

    assert out["status"] == "completed"
    assert len(out["plan"]) == 5
    assert out["queued_approvals"] == 1
    gid = out["goal_id"]

    goal = ctx.collab.conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
    assert goal["domain"] == "career"
    import json as _json
    meta = _json.loads(goal["career_meta"])
    assert meta["target_role"]

    focuses = ctx.collab.conn.execute(
        "SELECT * FROM learning_focuses WHERE goal_id=?", (gid,)).fetchall()
    assert len(focuses) >= 1

    milestones = ctx.collab.conn.execute(
        "SELECT * FROM milestones WHERE goal_id=?", (gid,)).fetchall()
    assert len(milestones) >= 4

    pending = ctx.store.list_approvals("pending")
    # agent-gated calls always land as action_type="tool_call"; the real
    # tool name lives in payload["tool"] (see amy/automation/executors.py's
    # agent_gate()).
    batch = [a for a in pending if a["payload"].get("tool") == "plane_batch_create_tasks"]
    assert len(batch) == 1
    assert batch[0]["tier"] == 2   # external -> hard tier-2 even by default


def test_career_template_disabled_by_kill_switch_falls_back_to_generic(ctx, monkeypatch):
    monkeypatch.setenv("AMY_AGENT_CAREER_GOAL", "0")
    ctx.llm = StubLLM([{"final": "no plan"}])   # generic path: no plan -> error, proves it ran
    out = run_goal(ctx, "Become a GenAI Engineer in 2 months")
    assert "error" in out   # generic planner ran (and failed to produce a plan), not the template
    careers = ctx.collab.conn.execute(
        "SELECT COUNT(*) n FROM goals WHERE domain='career'").fetchone()["n"]
    assert careers == 0


def test_non_career_goal_still_uses_generic_planner(ctx):
    ctx.llm = StubLLM([{"plan": ["Review budgets"], "reasoning": "test"},
                       {"step_done": "reviewed"},
                       {"summary": "done"}])
    out = run_goal(ctx, "cut my spending 10% this quarter")
    assert out["plan"] == ["Review budgets"]
    careers = ctx.collab.conn.execute(
        "SELECT COUNT(*) n FROM goals WHERE domain='career'").fetchone()["n"]
    assert careers == 0


# ---------------------------------------------------------------------------
# career_goal reactive agent — propose on trend, skip if already active
# ---------------------------------------------------------------------------

def _seed_trending_focus(ctx, topic: str) -> str:
    from amy.learning_feed.sensor import add_focus
    focus_id = add_focus(ctx.collab.conn, ctx.user_id, topic)
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    ctx.collab.conn.execute(
        "INSERT INTO activities(ts,kind,detail,domain) VALUES(?,?,?,?)",
        (now, "topic", topic, topic))
    ctx.collab.conn.commit()
    return focus_id


def test_career_goal_agent_proposes_when_trending_and_no_active_goal(ctx):
    events = ctx.events()
    register_reactive_agents(events, ctx)
    focus_id = _seed_trending_focus(ctx, "GenAI Engineer")

    events.emit("learning.feed_refreshed",
               {"focus": "GenAI Engineer", "focus_id": focus_id}, source="test")

    pending = ctx.store.list_approvals("pending")
    proposals = [a for a in pending if a["dedup_key"] == "career_goal_suggest"]
    assert len(proposals) == 1
    assert proposals[0]["payload"]["args"]["domain"] == "career"


def test_career_goal_agent_skips_when_active_goal_exists(ctx):
    from amy.autonomous import GoalEngine
    GoalEngine(ctx.collab).create_goal("Already on a career goal", domain="career")

    events = ctx.events()
    register_reactive_agents(events, ctx)
    focus_id = _seed_trending_focus(ctx, "Data Scientist")
    events.emit("learning.feed_refreshed",
               {"focus": "Data Scientist", "focus_id": focus_id}, source="test")

    pending = ctx.store.list_approvals("pending")
    assert not [a for a in pending if a["dedup_key"] == "career_goal_suggest"]


def test_career_goal_agent_skips_non_role_shaped_topic(ctx):
    events = ctx.events()
    register_reactive_agents(events, ctx)
    focus_id = _seed_trending_focus(ctx, "watercolor painting")
    events.emit("learning.feed_refreshed",
               {"focus": "watercolor painting", "focus_id": focus_id}, source="test")

    pending = ctx.store.list_approvals("pending")
    assert not [a for a in pending if a["dedup_key"] == "career_goal_suggest"]


# ---------------------------------------------------------------------------
# career_goal stall check — fires once in the window, not before or after
# ---------------------------------------------------------------------------

def _make_stale_career_goal(ctx, days_old: int) -> str:
    from amy.autonomous import GoalEngine
    gid = GoalEngine(ctx.collab).create_goal("Stale career goal", domain="career")
    created = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days_old)).isoformat()
    ctx.collab.conn.execute("UPDATE goals SET created_at=? WHERE id=?", (created, gid))
    ctx.collab.conn.commit()
    return gid


def test_stall_check_nudges_within_window(ctx, monkeypatch):
    monkeypatch.setenv("AMY_CAREER_STALL_DAYS", "5")
    _make_stale_career_goal(ctx, days_old=6)   # 1 day into the 3-day window
    out = career_goal_stall_check(ctx.events(), ctx)
    assert out["nudged"] == 1

    # same day again: exists_today gate suppresses the repeat (non-nag)
    out2 = career_goal_stall_check(ctx.events(), ctx)
    assert out2["nudged"] == 0


def test_stall_check_silent_before_threshold(ctx, monkeypatch):
    monkeypatch.setenv("AMY_CAREER_STALL_DAYS", "5")
    _make_stale_career_goal(ctx, days_old=2)
    out = career_goal_stall_check(ctx.events(), ctx)
    assert out["nudged"] == 0


def test_stall_check_silent_after_window_closes(ctx, monkeypatch):
    monkeypatch.setenv("AMY_CAREER_STALL_DAYS", "5")
    _make_stale_career_goal(ctx, days_old=30)   # long past the 3-day window
    out = career_goal_stall_check(ctx.events(), ctx)
    assert out["nudged"] == 0
