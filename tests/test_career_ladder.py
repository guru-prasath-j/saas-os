"""CAREER AUTOPILOT Part 5F — career ladder: target_role (applying next,
drives scouting/ATS/drafts) vs north_star_role (destination, drives
learning/milestones/portfolio). No LLM, no live MCP anywhere.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.automation.executors import execute
from amy.automation.orchestrator import (_extract_role_and_deadline,
                                         _weekly_milestones, run_goal)
from amy.collab import CollabDB


@pytest.fixture()
def ctx(tmp_path):
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-ladder", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    monkeypatch.setattr("amy.agents.reactive._get_llm", lambda ctx: None)


# --- parsing -----------------------------------------------------------------

def test_ladder_fallback_split_without_llm(ctx):
    ctx.llm = None
    role, _deadline, _weeks, north = _extract_role_and_deadline(
        ctx, "become an AI Mobile Engineer then a GenAI Engineer")
    assert "AI Mobile Engineer" in role
    assert north == "GenAI Engineer"


def test_no_ladder_when_roles_identical(ctx):
    ctx.llm = None
    _role, _d, _w, north = _extract_role_and_deadline(
        ctx, "become a GenAI Engineer then a GenAI Engineer")
    assert north is None


def test_plain_goal_has_no_north_star(ctx):
    ctx.llm = None
    _role, _d, _w, north = _extract_role_and_deadline(
        ctx, "become a GenAI Engineer in 2 months")
    assert north is None


# --- template ------------------------------------------------------------------

def test_ladder_goal_template_stores_both_roles_and_learns_toward_star(ctx):
    ctx.llm = None
    out = run_goal(ctx, "become an AI Mobile Engineer then a GenAI Engineer")
    assert out["status"] == "completed"
    gid = out["goal_id"]

    meta = json.loads(ctx.collab.conn.execute(
        "SELECT career_meta FROM goals WHERE id=?", (gid,)).fetchone()["career_meta"])
    assert "AI Mobile Engineer" in meta["target_role"]
    assert meta["north_star_role"] == "GenAI Engineer"

    # learning focuses aim at the DESTINATION (no jobspy connector in tests,
    # so gaps degrade to [learn_role] — which must be the north star)
    focuses = [r["topic"] for r in ctx.collab.conn.execute(
        "SELECT topic FROM learning_focuses WHERE goal_id=?", (gid,)).fetchall()]
    assert any("GenAI" in t for t in focuses)


def test_weekly_milestones_split_roles_by_phase():
    ms = _weekly_milestones("AI Mobile Engineer", 8, ["rag"],
                            learn_role="GenAI Engineer")
    portfolio = [m for m in ms if "Portfolio project" in m]
    applications = [m for m in ms if "Applications" in m]
    assert portfolio and all("GenAI Engineer" in m for m in portfolio)
    assert applications and all("AI Mobile Engineer" in m for m in applications)


# --- portfolio aims at the north star --------------------------------------------

def test_portfolio_analyze_prefers_north_star(ctx, monkeypatch):
    from amy.agents.reactive import portfolio_analyze
    from amy.events.store import EventStore

    gid = "gl1"
    ctx.collab.conn.execute(
        "INSERT INTO goals(id,title,domain,status,created_at,career_meta)"
        " VALUES(?,?,?,?,datetime('now'),?)",
        (gid, "ladder goal", "career", "active",
         json.dumps({"target_role": "AI Mobile Engineer",
                     "north_star_role": "GenAI Engineer"})))
    ctx.collab.conn.commit()

    captured = {}

    def fake_invoke(c, name, args, actor="human"):
        if name == "portfolio_repo_list":
            return {"repos": [{"name": "r1", "description": "genai rag demo"}]}
        if name == "job_search":
            captured["search_term"] = args.get("search_term")
            return {"jobs": []}
        return {"status": "pending"}

    monkeypatch.setattr("amy.tools.invoke", fake_invoke)
    monkeypatch.setattr("amy.saas.tenancy.resolve_vault_dir",
                        lambda uid: Path(ctx.finance_path).parent / "vault")
    portfolio_analyze(EventStore(ctx.collab), ctx, goal_id=gid)
    assert captured["search_term"] == "GenAI Engineer"


# --- wind-down promotion ------------------------------------------------------------

def _seed_goal(ctx, gid, meta):
    ctx.collab.conn.execute(
        "INSERT INTO goals(id,title,domain,status,created_at,career_meta)"
        " VALUES(?,?,?,?,datetime('now'),?)",
        (gid, "ladder goal", "career", "active", json.dumps(meta)))
    ctx.collab.conn.commit()


def test_winddown_promotes_instead_of_closing_with_north_star(ctx):
    _seed_goal(ctx, "g-promote", {"target_role": "AI Mobile Engineer",
                                  "north_star_role": "GenAI Engineer"})
    out = execute(ctx, "career_wind_down",
                  {"goal_id": "g-promote", "accepted_application_id": "a1",
                   "withdraw_others": False,
                   "promote_to_role": "GenAI Engineer"})
    assert out["promoted_to"] == "GenAI Engineer"
    assert out["closed_goal"] is False

    goal = ctx.collab.conn.execute(
        "SELECT status, career_meta FROM goals WHERE id='g-promote'").fetchone()
    assert goal["status"] == "active"          # goal stays open
    meta = json.loads(goal["career_meta"])
    assert meta["target_role"] == "GenAI Engineer"
    assert "north_star_role" not in meta       # the star was reached for aiming
    profile = ctx.store.get_career_profile(ctx.user_id)
    assert profile["target_role"] == "GenAI Engineer"   # ATS/drafts follow


def test_winddown_still_closes_without_promotion(ctx):
    _seed_goal(ctx, "g-close", {"target_role": "GenAI Engineer"})
    out = execute(ctx, "career_wind_down",
                  {"goal_id": "g-close", "accepted_application_id": "a1",
                   "withdraw_others": False, "promote_to_role": None})
    assert out["closed_goal"] is True and out["promoted_to"] is None
    assert ctx.collab.conn.execute(
        "SELECT status FROM goals WHERE id='g-close'").fetchone()["status"] == "completed"


def test_accepted_offer_proposes_promotion_when_ladder_present(ctx):
    from amy.events.factory import get_events

    _seed_goal(ctx, "g-agent", {"target_role": "AI Mobile Engineer",
                                "north_star_role": "GenAI Engineer"})
    pid, _ = ctx.store.add_posting_if_new(ctx.user_id, {
        "title": "AI Mobile Engineer", "company": "Acme",
        "url": "https://a/1", "location": "BLR", "description": "d"})
    app_id = ctx.store.create_application(ctx.user_id, pid, channel="email")
    es = get_events(ctx.user_id, ctx.collab, ctx=ctx)
    ctx.store.update_application_status(ctx.user_id, app_id, "accepted", "human")
    es.emit("career.application_status_changed",
            {"application_id": app_id, "status": "accepted"}, source="career_ui")

    rows = [dict(r) for r in ctx.collab.conn.execute(
        "SELECT * FROM approvals WHERE action_type='career_wind_down'").fetchall()]
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["promote_to_role"] == "GenAI Engineer"
    assert "north star" in rows[0]["title"].lower()
