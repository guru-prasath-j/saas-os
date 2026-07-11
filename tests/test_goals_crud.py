"""Goals/milestones full CRUD + AI milestone suggestions (user decides —
suggest never writes). Planner-level + route-level (TestClient)."""
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.collab import CollabDB
from amy.collab.planner import PlannerAgent


@pytest.fixture()
def planner(tmp_path):
    cdb = CollabDB(str(tmp_path / "collab.db"))
    yield PlannerAgent(cdb)
    cdb.close()


# --- planner level -------------------------------------------------------------

def test_update_and_delete_goal(planner):
    gid = planner.create_goal("Learn woodworking", domain="general")
    assert planner.update_goal(gid, title="Master woodworking") is True
    assert planner.get_plan(gid)["title"] == "Master woodworking"
    assert planner.update_goal(gid) is False          # nothing to change
    assert planner.update_goal("nope", title="x") is False

    planner.add_milestone(gid, "Buy tools")
    assert planner.delete_goal(gid) is True
    assert planner.get_plan(gid) is None
    assert planner.db.execute(
        "SELECT COUNT(*) c FROM milestones WHERE goal_id=?", (gid,)).fetchone()["c"] == 0
    assert planner.delete_goal(gid) is False


def test_update_and_delete_milestone_recomputes_progress(planner):
    gid = planner.create_goal("Run a 10k")
    m1 = planner.add_milestone(gid, "Run 3k")
    m2 = planner.add_milestone(gid, "Run 5k")
    planner.complete_milestone(m1)
    assert planner.get_plan(gid)["progress"] == 50.0

    assert planner.update_milestone(m2, "Run 7k") is True
    assert planner.update_milestone(m2, "   ") is False
    titles = [m["title"] for m in planner.get_plan(gid)["milestones"]]
    assert "Run 7k" in titles

    # deleting the incomplete one -> 100%
    assert planner.delete_milestone(m2) is True
    assert planner.get_plan(gid)["progress"] == 100.0
    # deleting the last one -> progress resets, no stale percentage
    planner.delete_milestone(m1)
    assert planner.get_plan(gid)["progress"] == 0
    assert planner.delete_milestone(m1) is False


def test_delete_goal_unlinks_learning_focus(planner):
    # learning_focuses is created lazily by AutomationStore — simulate it
    planner.db.execute(
        "CREATE TABLE IF NOT EXISTS learning_focuses (id TEXT PRIMARY KEY,"
        " uid TEXT, topic TEXT, goal_id TEXT, active INTEGER, created_at TEXT)")
    gid = planner.create_goal("Become a beekeeper")
    planner.db.execute(
        "INSERT INTO learning_focuses(id,uid,topic,goal_id,active,created_at)"
        " VALUES('f1','u','bees',?,1,datetime('now'))", (gid,))
    planner.db.commit()
    planner.delete_goal(gid)
    row = planner.db.execute(
        "SELECT goal_id, active FROM learning_focuses WHERE id='f1'").fetchone()
    assert row["goal_id"] is None and row["active"] == 1   # kept, just unlinked


# --- routes -------------------------------------------------------------------

@pytest.fixture()
def app_client():
    data_dir = tempfile.mkdtemp(prefix="amy_goals_routes_test_")
    os.environ["AMY_SAAS_DATA"] = data_dir
    from amy.saas.app import app
    from amy.saas.db import init_db
    from amy.saas import tenancy
    init_db()
    from fastapi.testclient import TestClient
    c = TestClient(app)
    email = f"goals-{uuid.uuid4().hex[:8]}@test.com"
    r = c.post("/auth/signup", json={"email": email, "password": "test1234"})
    assert r.status_code == 200, r.text
    tenancy.ensure_dirs(r.json()["user"]["id"])
    return c, {"Authorization": f"Bearer {r.json()['token']}"}


def test_goal_and_milestone_crud_routes(app_client):
    c, h = app_client
    gid = c.post("/api/goals", headers=h, json={"title": "Route goal"}).json()["id"]

    r = c.patch(f"/api/goals/{gid}", headers=h, json={"title": "Renamed goal"})
    assert r.status_code == 200 and r.json()["plan"]["title"] == "Renamed goal"
    assert c.patch("/api/goals/nope", headers=h,
                   json={"title": "x"}).status_code == 404

    mid = c.post(f"/api/goals/{gid}/milestones", headers=h,
                 json={"title": "First step"}).json()["id"]
    assert c.patch(f"/api/milestones/{mid}", headers=h,
                   json={"title": "First real step"}).status_code == 200
    assert c.delete(f"/api/milestones/{mid}", headers=h).status_code == 200
    assert c.delete(f"/api/milestones/{mid}", headers=h).status_code == 404

    assert c.delete(f"/api/goals/{gid}", headers=h).status_code == 200
    assert c.delete(f"/api/goals/{gid}", headers=h).status_code == 404


def test_suggest_returns_but_never_writes(app_client, monkeypatch):
    c, h = app_client
    monkeypatch.setattr("amy.saas.routers.collab._suggest_titles",
                        lambda goal, existing, user: ["Do A", "Do B", "Do C"])
    gid = c.post("/api/goals", headers=h, json={"title": "Suggested goal"}).json()["id"]
    r = c.post(f"/api/goals/{gid}/milestones/suggest", headers=h)
    assert r.status_code == 200
    assert r.json()["suggestions"] == ["Do A", "Do B", "Do C"]
    # nothing was added — the user decides
    goals = c.get("/api/goals", headers=h).json()["goals"]
    me = next(g for g in goals if g["id"] == gid)
    assert me["milestones"] == []
    assert c.post("/api/goals/nope/milestones/suggest", headers=h).status_code == 404


def test_suggest_fallback_is_deterministic_without_llm(app_client, monkeypatch):
    c, h = app_client
    # make the LLM constructor blow up -> the fallback list must come back
    import amy.saas.routers.collab as collab_mod
    monkeypatch.setattr("amy.llm.LLMRouter",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no llm")))
    gid = c.post("/api/goals", headers=h, json={"title": "Offline goal"}).json()["id"]
    r = c.post(f"/api/goals/{gid}/milestones/suggest", headers=h)
    assert r.status_code == 200
    assert len(r.json()["suggestions"]) >= 4
