"""CONTEXT_PLAN C1 — geo store transitions + errand reactive agent."""
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.agents.reactive import register_reactive_agents
from amy.automation import build_ctx
from amy.collab import CollabDB
from amy.events.store import EventStore
from amy.geo import GeoStore, haversine_m

# ~500 m apart in Chennai; 30 m point is inside a 150 m radius
HOME = (13.0827, 80.2707)
NEAR_30M = (13.08297, 80.2707)          # ≈30 m north
EDGE_170M = (13.08423, 80.2707)         # ≈170 m — outside r, inside 1.3r
FAR_500M = (13.0872, 80.2707)           # ≈500 m


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    cdb = CollabDB(str(tmp_path / "collab.db"))
    ctx = build_ctx("u-geo", "t@example.com", cdb, tmp_path, llm_router=None)
    es = EventStore(cdb)
    registered = register_reactive_agents(es, ctx)
    assert "errand" in registered
    yield ctx, es, cdb
    cdb.close()


def _add_task(cdb, title, place_tag=""):
    tid = uuid.uuid4().hex[:8]
    cdb.conn.execute(
        "INSERT INTO tasks (id, goal_id, title, done, created_at, place_tag)"
        " VALUES (?,?,?,0,datetime('now'),?)", (tid, "", title, place_tag))
    cdb.conn.commit()
    return tid


def test_haversine_sanity():
    assert haversine_m(*HOME, *HOME) == 0
    assert 400 < haversine_m(*HOME, *FAR_500M) < 600


def test_enter_and_leave_with_hysteresis(env):
    _, _, cdb = env
    gs = GeoStore(cdb)
    pid = gs.add_place("Ratnadeep", *HOME, kind="grocery", radius_m=150)

    r1 = gs.ingest_location(*NEAR_30M)
    assert [p["id"] for p in r1["entered"]] == [pid]
    assert len(gs.open_visits()) == 1

    # just past the radius but within the 1.3× leave band → still inside
    r2 = gs.ingest_location(*EDGE_170M)
    assert r2["left"] == [] and [p["id"] for p in r2["inside"]] == [pid]

    r3 = gs.ingest_location(*FAR_500M)
    assert [p["id"] for p in r3["left"]] == [pid]
    assert gs.open_visits() == []
    visits = gs.recent_visits()
    assert len(visits) == 1 and visits[0]["left_at"]

    fix = gs.last_fix()
    assert fix and abs(fix["lat"] - FAR_500M[0]) < 1e-9


def test_reenter_opens_new_visit(env):
    _, _, cdb = env
    gs = GeoStore(cdb)
    gs.add_place("Gym", *HOME, kind="gym", radius_m=150)
    gs.ingest_location(*NEAR_30M)
    gs.ingest_location(*FAR_500M)
    r = gs.ingest_location(*NEAR_30M)
    assert len(r["entered"]) == 1
    assert len(gs.recent_visits()) == 2


def test_errand_agent_keyword_match(env):
    """place_entered + open task whose title mentions the place kind →
    notification + agent.insight with reasoning."""
    ctx, es, cdb = env
    tid = _add_task(cdb, "Buy groceries for the week")
    es.emit("context.place_entered",
            {"place_id": "p1", "name": "Ratnadeep", "kind": "grocery"},
            source="test")

    notifs = ctx.notify_store().list()
    errands = [n for n in notifs if n["type"] == "errand_reminder"]
    assert len(errands) == 1
    assert "Ratnadeep" in errands[0]["title"]
    assert "Buy groceries" in errands[0]["body"]

    insights = es.recent("agent.insight")
    assert insights and insights[0]["payload"]["agent"] == "errand"
    assert insights[0]["payload"]["reasoning"]
    assert insights[0]["payload"]["task_id"] == tid


def test_errand_agent_place_tag_match(env):
    """An explicit place_tag matches even when the title shares no words."""
    ctx, es, cdb = env
    _add_task(cdb, "Pick up the parcel", place_tag="grocery")
    es.emit("context.place_entered",
            {"place_id": "p1", "name": "Ratnadeep", "kind": "grocery"},
            source="test")
    notifs = ctx.notify_store().list()
    assert any(n["type"] == "errand_reminder" and "parcel" in n["body"]
               for n in notifs)


def test_errand_agent_dedups_and_skips_done(env):
    ctx, es, cdb = env
    tid = _add_task(cdb, "Buy groceries")
    ev = {"place_id": "p1", "name": "Ratnadeep", "kind": "grocery"}
    es.emit("context.place_entered", ev, source="test")
    es.emit("context.place_entered", ev, source="test")   # same day → dedup
    notifs = [n for n in ctx.notify_store().list()
              if n["type"] == "errand_reminder"]
    assert len(notifs) == 1

    # completed tasks never remind
    cdb.conn.execute("UPDATE tasks SET done=1 WHERE id=?", (tid,))
    cdb.conn.commit()
    _add_task(cdb, "unrelated thing")
    es.emit("context.place_entered",
            {"place_id": "p2", "name": "Another Store", "kind": "grocery"},
            source="test")
    notifs2 = [n for n in ctx.notify_store().list()
               if n["type"] == "errand_reminder"]
    assert len(notifs2) == 1
