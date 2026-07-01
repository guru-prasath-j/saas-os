"""OL-2 — sensor base + registry; GitHubSensor still works as a Sensor."""
import os, tempfile
import pytest
from amy.collab.db import CollabDB
from amy.events.store import EventStore
from amy.operational import Sensor, SensorRegistry
from amy.sensors import GitHubSensor, NEW_ISSUE


@pytest.fixture
def es():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    d = CollabDB(p); yield EventStore(d); d.close(); os.unlink(p)


def test_github_sensor_is_a_sensor(es):
    gh = GitHubSensor(es)
    assert isinstance(gh, Sensor)
    assert gh.name == "github"


def test_registry_register_and_route(es):
    reg = SensorRegistry()
    reg.register(GitHubSensor(es))
    assert "github" in reg.names()
    ev = reg.ingest_webhook("github", "issues", {
        "repository": {"full_name": "me/proj"}, "sender": {"login": "me"},
        "action": "opened", "issue": {"title": "Bug", "html_url": "http://x", "number": 1}})
    assert ev.type == NEW_ISSUE
    assert len(es.recent(NEW_ISSUE)) == 1


def test_base_sensor_publishes(es):
    class TempSensor(Sensor):
        name = "temp"
        def poll(self):
            self.publish("temp.tick", {"n": 1}); return ["temp.tick"]
    reg = SensorRegistry(); reg.register(TempSensor(es))
    assert reg.poll("temp") == ["temp.tick"]
    assert len(es.recent("temp.tick")) == 1


def test_unknown_sensor_raises(es):
    with pytest.raises(KeyError):
        SensorRegistry().ingest_webhook("nope", "x", {})
