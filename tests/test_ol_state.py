"""OL-1 — operational models + state store."""
import os, tempfile
import pytest
from amy.collab.db import CollabDB
from amy.operational import StateStore, EntityState
from amy.operational.models import OperationalEvent
from amy.events.store import EventStore


@pytest.fixture
def db():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    d = CollabDB(p); yield d; d.close(); os.unlink(p)


def test_upsert_and_get_entity(db):
    s = StateStore(db)
    e = EntityState("github:repo:me/piOS", "repo", "github", "piOS", {"open_prs": 2})
    s.upsert_entity(e)
    got = s.get_entity("github:repo:me/piOS")
    assert got.title == "piOS" and got.state["open_prs"] == 2


def test_upsert_is_update(db):
    s = StateStore(db)
    s.upsert_entity(EntityState("x", "repo", "github", "old", {"v": 1}))
    s.upsert_entity(EntityState("x", "repo", "github", "new", {"v": 2}))
    assert s.count_entities() == 1
    assert s.get_entity("x").state["v"] == 2


def test_list_filters(db):
    s = StateStore(db)
    s.upsert_entity(EntityState("a", "repo", "github", "r"))
    s.upsert_entity(EntityState("b", "thread", "email", "t"))
    assert len(s.list_entities(kind="repo")) == 1
    assert len(s.list_entities(source="email")) == 1
    assert len(s.list_entities()) == 2


def test_delete_entity(db):
    s = StateStore(db)
    s.upsert_entity(EntityState("a", "repo", "github", "r"))
    assert s.delete_entity("a") is True
    assert s.get_entity("a") is None


def test_connector_state_roundtrip(db):
    s = StateStore(db)
    s.set_connector_state("github", status="running", health="ok", cursor="c1")
    st = s.get_connector_state("github")
    assert st["status"] == "running" and st["health"] == "ok" and st["cursor"] == "c1"
    # partial update preserves other fields
    s.set_connector_state("github", health="degraded")
    st2 = s.get_connector_state("github")
    assert st2["status"] == "running" and st2["health"] == "degraded"
    assert len(s.all_connector_states()) == 1


def test_operational_event_publishes_on_one_bus(db):
    es = EventStore(db)
    eid = OperationalEvent("github.NEW_COMMIT", {"repo": "me/piOS"}, source="github").publish(es)
    rows = es.recent("github.NEW_COMMIT")
    assert len(rows) == 1 and rows[0]["id"] == eid


def test_reset_clears_op_tables(db):
    s = StateStore(db)
    s.upsert_entity(EntityState("a", "repo", "github", "r"))
    s.set_connector_state("github", status="running")
    db.reset()
    assert s.count_entities() == 0
    assert s.all_connector_states() == []
