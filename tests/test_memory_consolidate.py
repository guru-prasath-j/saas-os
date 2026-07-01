"""Phase 5 — weekly consolidation from daily notes."""
import datetime as _dt
import tempfile
from pathlib import Path
import pytest
from amy.memory import MemoryWriter, Consolidator, WEEKLY_DIR, EntityIndex


@pytest.fixture
def vault():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def _seed(vault, when):
    """Write a few events on a given day via the real writer."""
    idx = EntityIndex()
    idx.add("PiOS")
    w = MemoryWriter(vault, entity_index=idx)
    w.log_event({"id": "q1", "type": "query.asked",
                 "payload": {"query": "how is PiOS?", "answer": "good"},
                 "ts": when.isoformat()})
    w.log_event({"id": "d1", "type": "decision.recorded",
                 "payload": {"title": "Ship PiOS v1", "category": "projects", "confidence": 0.7},
                 "ts": when.isoformat()})
    w.log_event({"id": "g1", "type": "goal.created",
                 "payload": {"title": "Launch beta", "domain": "projects"},
                 "ts": when.isoformat()})


def test_patterns_aggregates_week(vault):
    now = _dt.datetime.now(_dt.timezone.utc)
    _seed(vault, now)
    p = Consolidator(vault).patterns(now.date())
    assert p["total_entries"] == 3
    assert p["active_days"] == 1
    assert p["by_kind"]["chat"] == 1
    assert "Ship PiOS v1" in p["decisions"]
    assert "Launch beta" in p["new_goals"]
    assert "PiOS" in p["top_links"]


def test_weekly_note_written(vault):
    now = _dt.datetime.now(_dt.timezone.utc)
    _seed(vault, now)
    res = Consolidator(vault).weekly(now.date())
    assert res["written"] is True
    note = vault / res["path"]
    assert note.exists()
    body = note.read_text(encoding="utf-8")
    assert "type: weekly" in body
    assert "Ship PiOS v1" in body
    assert "Launch beta" in body
    assert WEEKLY_DIR in res["path"]


def test_weekly_empty_week(vault):
    res = Consolidator(vault).weekly(_dt.date(2030, 1, 1))
    assert res["written"] is False
    assert res["path"] is None


def test_weekly_idempotent_overwrite(vault):
    now = _dt.datetime.now(_dt.timezone.utc)
    _seed(vault, now)
    c = Consolidator(vault)
    c.weekly(now.date())
    c.weekly(now.date())  # re-run
    weeks = list((vault / WEEKLY_DIR).glob("*.md"))
    assert len(weeks) == 1  # single derived note, not duplicated
