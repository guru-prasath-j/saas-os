"""Event layer + digital twin tests. Offline.

Run:  pytest tests/test_events_twin.py -v
"""
import os
import tempfile

from amy.vault import Note
from amy.collab import CollabDB, CollabMaster, PlannerAgent
from amy.events import EventStore, register_default_triggers
from amy.collab.memory import MemoryManager
from amy.twin import DigitalTwin


def _n(path, title, body):
    return Note(path=path, title=title, meta={"tags": []}, body=body)


VAULT = [
    _n("Projects/app.md", "Cool App", "# Cool App\n\nbuilt a flutter app with a python backend"),
    _n("Learning/python.md", "Python", "# Python\n\nlearning python and ai, course notes"),
    _n("Finance/budget.md", "Budget", "# Budget\n\nmonthly budget and savings money"),
]


def _db():
    return CollabDB(os.path.join(tempfile.mkdtemp(prefix="amy_evt_"), "collab.db"))


# --- event store / bus ------------------------------------------------------
def test_emit_persist_and_read():
    ev = EventStore(_db())
    ev.emit("query.asked", {"query": "hi"}, source="test")
    ev.emit("goal.created", {"title": "g"}, source="test")
    assert len(ev.recent()) == 2
    assert ev.recent(event_type="goal.created")[0]["payload"]["title"] == "g"
    assert ev.stats()["query.asked"] == 1


def test_subscriber_fires_on_emit():
    ev = EventStore(_db())
    seen = []
    ev.subscribe("goal.completed", lambda e: seen.append(e["payload"]["title"]))
    ev.emit("goal.completed", {"title": "Ship it"})
    assert seen == ["Ship it"]


def test_default_trigger_writes_memory():
    db = _db()
    ev = EventStore(db)
    mem = MemoryManager(db)
    register_default_triggers(ev, mem)
    ev.emit("goal.completed", {"title": "Launch"})
    assert any("Launch" in s["text"] for s in mem.recent_summaries(5))


def test_planner_emits_goal_events():
    db = _db()
    ev = EventStore(db)
    pl = PlannerAgent(db, events=ev)
    g = pl.create_goal("Save money", "finance")
    m = pl.add_milestone(g, "step 1")
    pl.complete_milestone(m)            # 100% -> goal.completed
    types = [e["type"] for e in ev.recent()]
    assert "goal.created" in types
    assert "goal.completed" in types


def test_collabmaster_emits_query_event():
    cm = CollabMaster(VAULT, os.path.join(tempfile.mkdtemp(prefix="amy_evt_"), "collab.db"), llm=None)
    cm.handle("how is my budget")
    assert any(e["type"] == "query.asked" for e in cm.events.recent())
    cm.close()


# --- digital twin -----------------------------------------------------------
def test_twin_snapshot_composes_layers():
    db = _db()
    PlannerAgent(db).create_goal("Learn Rust", "learning")
    twin = DigitalTwin(VAULT, db)
    snap = twin.snapshot()
    assert set(("profile", "memory", "goals", "traits")) <= set(snap)
    assert snap["profile"]["skills"]
    assert "focus_areas" in snap["traits"]
    assert any(g["title"] == "Learn Rust" for g in snap["goals"])
    db.close()


def test_twin_ask_returns_user_facts():
    db = _db()
    twin = DigitalTwin(VAULT, db)              # no llm -> facts summary
    res = twin.ask("what am I focused on?")
    assert "answer" in res and res["answer"]
    assert "facts" in res
    db.close()


def test_scheduler_generate_and_store():
    from amy.events.scheduler import generate_and_store
    db = _db()
    PlannerAgent(db).create_goal("Ship it", "projects")   # an open goal -> a suggestion
    digest = generate_and_store(db)
    assert "suggestions" in digest and "open_goals" in digest
    latest = EventStore(db).recent("digest.generated", 1)
    assert latest and latest[0]["source"] == "scheduler"
    assert "suggestion_count" in latest[0]["payload"]
    db.close()
