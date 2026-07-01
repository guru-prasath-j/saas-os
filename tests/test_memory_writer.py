"""Phase 1 tests for the Memory Writer (Journaler). Offline, temp vault."""
import datetime as _dt
import tempfile
from pathlib import Path

import pytest

from amy.memory import MemoryWriter, DAILY_DIR, MEMORY_DIR


@pytest.fixture
def vault():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def _ev(eid, etype, payload, ts=None):
    return {"id": eid, "type": etype, "payload": payload,
            "ts": ts or _dt.datetime.now(_dt.timezone.utc).isoformat(), "source": "test"}


def test_daily_note_created_and_appended(vault):
    w = MemoryWriter(vault)
    w.log_event(_ev("e1", "query.asked", {"query": "hello", "answer": "hi there"}))
    files = list((vault / DAILY_DIR).glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "type: daily" in text
    assert "**Q:** hello" in text
    assert "eid:e1" in text


def test_idempotent_daily(vault):
    w = MemoryWriter(vault)
    assert w.append_daily("chat", "x", "dup") is True
    assert w.append_daily("chat", "x", "dup") is False  # second time skipped
    files = list((vault / DAILY_DIR).glob("*.md"))
    text = files[0].read_text(encoding="utf-8")
    assert text.count("eid:dup") == 1


def test_decision_makes_atomic_note(vault):
    w = MemoryWriter(vault)
    res = w.log_event(_ev("d1", "decision.recorded",
                          {"title": "Take the job", "category": "career",
                           "confidence": 0.8, "reason": "more pay"}))
    assert res["daily"] is True
    assert res["atomic"] is not None
    atomic = vault / res["atomic"]
    assert atomic.exists()
    body = atomic.read_text(encoding="utf-8")
    assert "Take the job" in body
    assert "[[Career]]" in body          # link inserted
    assert "type: decision" in body
    assert "eid:d1" in body


def test_atomic_idempotent(vault):
    w = MemoryWriter(vault)
    w.log_event(_ev("d1", "decision.recorded", {"title": "Take the job", "category": "career"}))
    res2 = w.log_event(_ev("d1", "decision.recorded", {"title": "Take the job", "category": "career"}))
    # same eid -> atomic skipped
    assert res2["atomic"] is None


def test_github_event_journaled(vault):
    w = MemoryWriter(vault)
    res = w.log_event(_ev("g1", "github.CI_FAILURE",
                          {"repo": "me/proj", "title": "tests failed", "url": "http://x"}))
    files = list((vault / DAILY_DIR).glob("*.md"))
    text = files[0].read_text(encoding="utf-8")
    assert "GitHub ci failure" in text
    assert "me/proj" in text
    # CI failure is not an atomic note
    assert res["atomic"] is None


def test_github_release_makes_atomic(vault):
    w = MemoryWriter(vault)
    res = w.log_event(_ev("g2", "github.NEW_RELEASE",
                          {"repo": "me/proj", "title": "v1.0", "url": "http://x"}))
    assert res["atomic"] is not None


def test_capture_atomic_and_link_block(vault):
    w = MemoryWriter(vault)
    res = w.log_event(_ev("c1", "capture.added",
                          {"title": "Whiteboard", "caption": "architecture sketch"}))
    assert res["atomic"] is not None
    body = (vault / res["atomic"]).read_text(encoding="utf-8")
    assert "Whiteboard" in body
    assert "architecture sketch" in body


def test_entries_grouped_by_event_date(vault):
    w = MemoryWriter(vault)
    yesterday = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)).isoformat()
    w.log_event(_ev("a", "query.asked", {"query": "old"}, ts=yesterday))
    w.log_event(_ev("b", "query.asked", {"query": "new"}))
    files = sorted((vault / DAILY_DIR).glob("*.md"))
    assert len(files) == 2  # one per day


def test_generic_event_fallback(vault):
    w = MemoryWriter(vault)
    res = w.log_event(_ev("x1", "some.weird.event", {"k": "v"}))
    assert res["daily"] is True
    files = list((vault / DAILY_DIR).glob("*.md"))
    assert "some.weird.event" in files[0].read_text(encoding="utf-8")
