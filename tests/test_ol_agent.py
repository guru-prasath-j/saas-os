"""OL-7 — reference agent wiring (Career) over the Operational Layer."""
import os, tempfile
import pytest
from amy.collab.db import CollabDB
from amy.events.store import EventStore
from amy.operational import OperationalLayer, CareerOpsAgent


@pytest.fixture
def ops():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    d = CollabDB(p)
    o = OperationalLayer(d, EventStore(d), connector_dir=tempfile.mkdtemp())
    yield d, o
    d.close(); os.unlink(p)


def test_career_agent_reacts_to_github(ops):
    d, o = ops
    CareerOpsAgent(o).activate()
    o.publish("github.NEW_RELEASE", {"repo": "me/piOS", "url": "http://x"}, source="github")
    # it should upsert a career portfolio entity + emit career.application_updated
    ents = o.entities.list_entities(source="career")
    assert any(e.title == "me/piOS" for e in ents)
    assert len(o.events.recent("career.application_updated")) == 1


def test_career_agent_detects_interview(ops):
    d, o = ops
    CareerOpsAgent(o).activate()
    o.publish("calendar.NEW_EVENT", {"title": "Onsite interview at ACME"}, source="calendar")
    assert len(o.events.recent("career.interview_detected")) == 1


def test_career_agent_ignores_irrelevant_calendar(ops):
    d, o = ops
    CareerOpsAgent(o).activate()
    o.publish("calendar.NEW_EVENT", {"title": "Dentist"}, source="calendar")
    assert len(o.events.recent("career.interview_detected")) == 0


def test_agent_only_subscribes_declared(ops):
    d, o = ops
    a = CareerOpsAgent(o).activate()
    assert "github.NEW_RELEASE" in a.subscribes
    # an unrelated event does not create career state
    o.publish("github.NEW_COMMIT", {"repo": "x"}, source="github")
    assert o.entities.list_entities(source="career") == []
