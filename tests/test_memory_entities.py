"""Phase 3 tests — entity extraction + auto-linking into journal entries."""
import os
import tempfile
from pathlib import Path

import pytest

from amy.collab.db import CollabDB
from amy.events.store import EventStore
from amy.memory import EntityIndex, MemoryWriter, JournalSync, DAILY_DIR


class _Note:
    def __init__(self, title, category="projects"):
        self.title = title
        self.category = category
        self.body = ""
        self.path = title + ".md"


@pytest.fixture
def setup():
    fd, dbp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    vault = tempfile.mkdtemp()
    db = CollabDB(dbp)
    yield db, Path(vault)
    db.close()
    os.unlink(dbp)


def test_entity_index_from_notes_and_goals(setup):
    db, _ = setup
    db.conn.execute("INSERT INTO goals (id,title,domain,status) VALUES ('g1','Learn Rust','learning','active')")
    db.conn.commit()
    notes = [_Note("PiOS"), _Note("Machine Learning", "knowledge")]
    idx = EntityIndex.from_sources(notes=notes, collab_db=db)
    assert len(idx) >= 3  # PiOS, Machine Learning, Learn Rust


def test_extract_phrase_and_word_boundary():
    idx = EntityIndex()
    idx.add("Machine Learning")
    idx.add("PiOS")
    idx.add_tag("career")
    links, tags = idx.extract("Today I worked on PiOS and some machine learning. Big career step.")
    assert "PiOS" in links
    assert "Machine Learning" in links
    assert "career" in tags


def test_no_false_substring_match():
    idx = EntityIndex()
    idx.add("Cat")            # should not match inside "category"
    links, _ = idx.extract("I reviewed the category list.")
    assert "Cat" not in links


def test_writer_autolinks_entries(setup):
    db, vault = setup
    notes = [_Note("PiOS")]
    idx = EntityIndex.from_sources(notes=notes, collab_db=db)
    w = MemoryWriter(vault, entity_index=idx)
    w.log_event({"id": "e1", "type": "query.asked",
                 "payload": {"query": "how is PiOS going?", "answer": "great"},
                 "ts": None, "source": "chat"})
    text = list((vault / DAILY_DIR).glob("*.md"))[0].read_text(encoding="utf-8")
    assert "[[PiOS]]" in text


def test_journalsync_uses_notes(setup):
    db, vault = setup
    es = EventStore(db)
    es.emit("query.asked", {"query": "shipping PiOS today", "answer": "ok"}, source="chat")
    notes = [_Note("PiOS")]
    JournalSync(db, vault, notes=notes).sync()
    text = list((vault / DAILY_DIR).glob("*.md"))[0].read_text(encoding="utf-8")
    assert "[[PiOS]]" in text


def test_links_capped(setup):
    db, vault = setup
    idx = EntityIndex()
    for i in range(20):
        idx.add(f"Project{i}")
    w = MemoryWriter(vault, entity_index=idx)
    mention = " ".join(f"Project{i}" for i in range(20))
    w.log_event({"id": "e1", "type": "query.asked",
                 "payload": {"query": mention}, "ts": None, "source": "chat"})
    text = list((vault / DAILY_DIR).glob("*.md"))[0].read_text(encoding="utf-8")
    assert text.count("[[Project") <= 5  # max_links cap
