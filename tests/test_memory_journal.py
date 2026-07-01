"""Phase 2 tests — Event Bus ↔ MemoryWriter bridge (push + pull)."""
import os
import tempfile
from pathlib import Path

import pytest

from amy.collab.db import CollabDB
from amy.events.store import EventStore
from amy.memory import attach_journal, JournalSync, DAILY_DIR


@pytest.fixture
def setup():
    fd, dbp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    vault = tempfile.mkdtemp()
    db = CollabDB(dbp)
    yield db, Path(vault)
    db.close()
    os.unlink(dbp)


def test_push_attach_journal(setup):
    db, vault = setup
    es = EventStore(db)
    attach_journal(es, vault)
    es.emit("decision.recorded", {"title": "Move to Berlin", "category": "career"}, source="t")
    # daily note written live
    files = list((vault / DAILY_DIR).glob("*.md"))
    assert len(files) == 1
    assert "Move to Berlin" in files[0].read_text(encoding="utf-8")
    # atomic note for the decision
    assert list((vault / "09_Memory").glob("*.md"))


def test_pull_sync_from_events_table(setup):
    db, vault = setup
    es = EventStore(db)  # no journal attached — events only persist to the table
    es.emit("query.asked", {"query": "hi", "answer": "hello"}, source="chat")
    es.emit("goal.created", {"title": "Learn Rust", "domain": "learning"}, source="planner")
    # nothing written yet
    assert not (vault / DAILY_DIR).exists()
    res = JournalSync(db, vault).sync()
    assert res["written"] == 2
    assert res["atomic_notes"] >= 1  # goal.created -> atomic
    files = list((vault / DAILY_DIR).glob("*.md"))
    text = files[0].read_text(encoding="utf-8")
    assert "hi" in text and "Learn Rust" in text


def test_sync_is_idempotent(setup):
    db, vault = setup
    es = EventStore(db)
    es.emit("decision.recorded", {"title": "x", "category": "finance"}, source="t")
    js = JournalSync(db, vault)
    first = js.sync()
    second = js.sync()
    assert first["written"] == 1
    assert second["written"] == 0      # already journaled
    assert second["skipped"] == 1
    # no duplicate entries
    files = list((vault / DAILY_DIR).glob("*.md"))
    assert files[0].read_text(encoding="utf-8").count("eid:") == 1


def test_cursor_recorded(setup):
    db, vault = setup
    es = EventStore(db)
    eid = es.emit("goal.completed", {"title": "done"}, source="t")
    JournalSync(db, vault).sync()
    cur = db.conn.execute("SELECT value FROM prefs WHERE key='memory.journal_cursor'").fetchone()
    assert cur["value"] == eid
