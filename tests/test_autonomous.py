"""PIOS v2 Autonomous Core tests — event bus, goal engine, executive, unified memory.
Offline.

Run:  pytest tests/test_autonomous.py -v
"""
import json
import os
import tempfile

import pytest

from amy.vault import Note
from amy.collab import CollabDB
from amy.events import EventStore
from amy.autonomous import GoalEngine, ExecutiveAgent, UnifiedMemory


def _db():
    return CollabDB(os.path.join(tempfile.mkdtemp(prefix="amy_v2_"), "collab.db"))


# --- event bus: publish / subscribe / unsubscribe ---------------------------
def test_event_bus_publish_subscribe_unsubscribe():
    ev = EventStore(_db())
    seen = []
    handler = lambda e: seen.append(e["type"])
    ev.subscribe("ping", handler)
    ev.publish("ping", {"x": 1})
    assert seen == ["ping"]
    assert ev.unsubscribe("ping", handler) is True
    ev.publish("ping", {"x": 2})
    assert seen == ["ping"]          # handler no longer fires
    assert ev.unsubscribe("ping", handler) is False


# --- goal engine: tasks + dependencies --------------------------------------
def test_goal_engine_tasks_and_progress():
    ge = GoalEngine(_db())
    g = ge.create_goal("Ship app", "projects")
    ge.add_milestone(g, "design")
    t = ge.add_task(g, "write tests")
    assert ge.progress(g) == 0.0
    ge.complete_task(t)
    assert ge.progress(g) == 50.0   # 1 of 2 (milestone + task) done


def test_goal_engine_dependencies_and_blocked():
    ge = GoalEngine(_db())
    a = ge.create_goal("Foundation", "career")
    b = ge.create_goal("Launch", "career")
    ge.add_dependency(b, a)          # Launch depends on Foundation
    assert ge.is_blocked(b) is True  # Foundation not done
    m = ge.add_milestone(a, "done it")
    ge.complete_milestone(m)         # Foundation -> done
    assert ge.is_blocked(b) is False


def test_goal_engine_rejects_cycles():
    ge = GoalEngine(_db())
    a = ge.create_goal("A"); b = ge.create_goal("B")
    ge.add_dependency(b, a)
    with pytest.raises(ValueError):
        ge.add_dependency(a, b)      # would create a cycle


# --- executive agent --------------------------------------------------------
def test_executive_prioritizes_unblocked_first():
    db = _db(); ge = GoalEngine(db)
    free = ge.create_goal("Free goal", "projects")
    dep = ge.create_goal("Blocked goal", "projects")
    blocker = ge.create_goal("Blocker", "projects")
    ge.add_dependency(dep, blocker)
    ex = ExecutiveAgent(db)
    pris = ex.prioritize_goals()
    blocked = next(p for p in pris if p["id"] == dep)
    free_p = next(p for p in pris if p["id"] == free)
    assert free_p["priority"] > blocked["priority"]
    assert blocked["blocked"] is True


def test_executive_conflicts_and_coordination():
    db = _db(); ge = GoalEngine(db)
    ge.create_goal("Goal A", "career"); ge.create_goal("Goal B", "career")  # same domain
    ex = ExecutiveAgent(db)
    conflicts = ex.resolve_conflicts()
    assert any(c["type"] == "domain_contention" for c in conflicts)
    coord = ex.coordinate_agents()
    assert coord and coord[0]["agent"].endswith("_agent")


def test_executive_reprioritizes_domains():
    db = _db(); ge = GoalEngine(db)
    ge.create_goal("g", "finance")
    order = ExecutiveAgent(db).reprioritize_domains()
    assert any(d["domain"] == "finance" for d in order)


# --- unified memory ---------------------------------------------------------
def test_unified_memory_recall_merges_sources():
    db = _db()
    notes = [Note(path="Finance/budget.md", title="Budget", meta={"tags": []},
                  body="monthly budget and savings money")]
    cdir = tempfile.mkdtemp(prefix="amy_v2_conn_")
    json.dump([{"id": "1", "subject": "Budget review", "snippet": "your budget"}],
              open(os.path.join(cdir, "email.json"), "w"))
    db.conn.execute("INSERT INTO summaries (ts,text) VALUES (?,?)", ("t", "User: how is my budget\nAmy: ok"))
    db.conn.commit()
    um = UnifiedMemory(notes, db, connector_dir=cdir)
    res = um.recall("budget")
    assert any("budget" in v["path"].lower() for v in res["vault"])
    assert res["email"]                       # gmail-style source matched
    assert res["conversations"]               # conversation source matched
    assert res["count"] >= 2


# --- v3 autopilot (auto-execution loop) -------------------------------------
from amy.autonomous import Autopilot
from amy.product import Marketplace


def test_autopilot_enables_agent_for_top_goal():
    db = _db(); ge = GoalEngine(db); ge.create_goal("Ship", "projects")
    Marketplace(db).disable("projects_agent")
    rep = Autopilot(db).run()
    assert any(a["action"] == "enable_agent" and a["target"] == "projects_agent" for a in rep["actions"])
    assert "projects_agent" not in Marketplace(db).disabled_set()


def test_autopilot_advances_stalled_goal():
    db = _db(); ge = GoalEngine(db); g = ge.create_goal("Learn Rust", "learning")
    rep = Autopilot(db).run()
    assert any(a["action"] == "advance_goal" for a in rep["actions"])
    assert ge.list_tasks(g)            # starter tasks were added


def test_autopilot_dry_run_changes_nothing():
    db = _db(); ge = GoalEngine(db); g = ge.create_goal("Plan trip", "general")
    rep = Autopilot(db).run(dry_run=True)
    assert rep["dry_run"] is True and rep["count"] >= 1
    assert ge.list_tasks(g) == []      # preview only — nothing applied


def test_autopilot_logs_run_event():
    db = _db(); GoalEngine(db).create_goal("x", "general"); Autopilot(db).run()
    assert any(e["type"] == "autopilot.run" for e in EventStore(db).recent())
