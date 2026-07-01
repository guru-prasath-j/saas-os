"""Intelligence layer tests — Decision journal + Timeline. Offline.

Run:  pytest tests/test_intelligence.py -v
"""
import os
import tempfile

from amy.vault import Note
from amy.collab import CollabDB
from amy.events import EventStore
from amy.intelligence import DecisionEngine, TimelineEngine


def _db():
    return CollabDB(os.path.join(tempfile.mkdtemp(prefix="amy_int_"), "collab.db"))


def test_decision_record_outcome_list():
    db = _db()
    de = DecisionEngine(db, events=EventStore(db))
    d = de.record("Take the Flutter role", "best growth", "career", confidence=0.7)
    got = de.get(d)
    assert got["title"] == "Take the Flutter role" and got["status"] == "open" and got["confidence"] == 0.7
    de.set_outcome(d, "accepted offer", "resolved")
    assert de.get(d)["status"] == "resolved" and de.get(d)["outcome"] == "accepted offer"
    assert any(x["id"] == d for x in de.list())


def test_decision_emits_event():
    db = _db(); ev = EventStore(db)
    DecisionEngine(db, events=ev).record("x")
    assert any(e["type"] == "decision.recorded" for e in ev.recent())


def test_timeline_merges_sources_sorted():
    db = _db(); ev = EventStore(db)
    db.conn.execute("INSERT INTO activities (ts,kind,detail,domain) VALUES (?,?,?,?)",
                    ("2026-06-01T00:00:00+00:00", "query", "old q", "general"))
    db.conn.commit()
    ev.emit("query.asked", {"query": "newer"})
    DecisionEngine(db).record("a decision")
    notes = [Note(path="n.md", title="Note A", meta={"created": "2026-06-15T00:00:00+00:00"}, body="")]
    tl = TimelineEngine(db).build(notes=notes, limit=50)
    assert len(tl) >= 4
    assert tl == sorted(tl, key=lambda x: x["ts"], reverse=True)     # newest first
    assert {i["source"] for i in tl} >= {"activity", "event", "decision", "note"}


def test_timeline_grouping_filter_search_summary():
    db = _db()
    for ts, d in [("2026-06-10T09:00:00+00:00", "q1"), ("2026-06-10T11:00:00+00:00", "q2"),
                  ("2026-06-12T09:00:00+00:00", "q3")]:
        db.conn.execute("INSERT INTO activities (ts,kind,detail,domain) VALUES (?,?,?,?)",
                        (ts, "query", d, "general"))
    db.conn.commit()
    tl = TimelineEngine(db)
    days = tl.grouped("day")
    assert any(g["period"] == "2026-06-10" and g["count"] == 2 for g in days)
    assert all(i["source"] == "activity" for i in tl.build(sources=["activity"]))   # filter
    assert all("q3" in i["text"] for i in tl.build(query="q3"))                       # search
    assert tl.summary()["total"] >= 3
