"""OL-5 — OperationalLayer façade."""
import os, tempfile
import pytest
from amy.collab.db import CollabDB
from amy.events.store import EventStore
from amy.operational import OperationalLayer
from amy.operational.models import EntityState


@pytest.fixture
def ops():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    cdir = tempfile.mkdtemp()
    d = CollabDB(p)
    o = OperationalLayer(d, EventStore(d), connector_dir=cdir)
    yield d, o
    d.close(); os.unlink(p)


def test_one_bus_reused(ops):
    d, o = ops
    # publishing through the façade lands in the same events table
    eid = o.publish("github.NEW_COMMIT", {"repo": "x"}, source="github")
    assert o.events.recent("github.NEW_COMMIT")[0]["id"] == eid


def test_subscribe_receives(ops):
    d, o = ops
    got = []
    o.subscribe("test.x", lambda ev: got.append(ev))
    o.publish("test.x", {"n": 1})
    assert len(got) == 1


def test_register_default_sensors(ops):
    d, o = ops
    names = o.register_default_sensors()
    assert "github" in names
    assert o.connectors.status("github")["status"] == "running"


def test_snapshot_shape(ops):
    d, o = ops
    o.state.upsert_entity(EntityState("a", "repo", "github", "A"))
    o.publish("e.x", {})
    snap = o.snapshot()
    assert snap["entity_count"] == 1
    assert "e.x" in snap["event_types"]
    assert isinstance(snap["connectors"], list)


def test_replay_via_facade(ops):
    d, o = ops
    o.publish("a.x", {}); o.publish("a.y", {})
    got = []
    rep = o.replay(lambda ev: got.append(ev["type"]))
    assert rep["dispatched"] == 2
