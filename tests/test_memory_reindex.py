"""Phase 6 — vault-as-truth reindex / reconcile / rebuild."""
import datetime as _dt
import os
import tempfile
from pathlib import Path
import pytest
from amy.collab.db import CollabDB
from amy.events.store import EventStore
from amy.memory import MemoryWriter, JournalSync, VaultReindex


@pytest.fixture
def setup():
    fd, dbp = tempfile.mkstemp(suffix=".db"); os.close(fd)
    vault = tempfile.mkdtemp()
    db = CollabDB(dbp)
    yield db, Path(vault)
    db.close(); os.unlink(dbp)


def _emit_and_journal(db, vault):
    es = EventStore(db)
    es.emit("decision.recorded", {"title": "Ship v1", "category": "projects", "confidence": 0.7}, source="t")
    es.emit("query.asked", {"query": "status?", "answer": "ok"}, source="chat")
    JournalSync(db, vault).sync()


def test_scan_inventories_vault(setup):
    db, vault = setup
    _emit_and_journal(db, vault)
    inv = VaultReindex(vault).scan()
    assert inv["daily_eid_count"] == 2
    assert inv["memory_note_count"] == 1            # the decision atomic note
    assert inv["memory_notes"][0]["type"] == "decision"


def test_verify_in_sync(setup):
    db, vault = setup
    _emit_and_journal(db, vault)
    rep = VaultReindex(vault).verify(db)
    assert rep["in_sync"] is True
    assert rep["missing_from_db"] == []


def test_verify_detects_db_loss(setup):
    db, vault = setup
    _emit_and_journal(db, vault)
    db.conn.execute("DELETE FROM events"); db.conn.commit()  # simulate SQLite wipe
    rep = VaultReindex(vault).verify(db)
    assert rep["in_db"] == 0
    assert len(rep["missing_from_db"]) == 2   # vault still has them


def test_rebuild_decisions_from_vault(setup):
    db, vault = setup
    _emit_and_journal(db, vault)
    db.conn.execute("DELETE FROM decisions"); db.conn.commit()  # wipe structured state
    res = VaultReindex(vault).rebuild_decisions(db)
    assert res["rebuilt"] == 1
    row = db.conn.execute("SELECT title, domain, confidence FROM decisions").fetchone()
    assert row["title"] == "Ship v1"
    assert row["domain"] == "projects"
    assert abs(row["confidence"] - 0.7) < 1e-6


def test_rebuild_idempotent(setup):
    db, vault = setup
    _emit_and_journal(db, vault)
    rx = VaultReindex(vault)
    rx.rebuild_decisions(db)
    second = rx.rebuild_decisions(db)
    assert second["rebuilt"] == 0
    assert second["skipped"] >= 1
