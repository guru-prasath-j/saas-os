"""OL-3 — connector lifecycle + health over the real ConnectorRegistry."""
import os, tempfile
import pytest
from amy.collab.db import CollabDB
from amy.operational import StateStore, ConnectorManager


@pytest.fixture
def mgr():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    cdir = tempfile.mkdtemp()
    d = CollabDB(p)
    m = ConnectorManager(StateStore(d), connector_dir=cdir)
    yield m
    d.close(); os.unlink(p)


def test_lifecycle_transitions(mgr):
    mgr.register("email")
    assert mgr.status("email")["status"] == "registered"
    mgr.start("email")
    assert mgr.status("email")["status"] == "running"
    mgr.stop("email")
    assert mgr.status("email")["status"] == "stopped"


def test_health_check_local_ok(mgr):
    # local providers exist for email/calendar/tasks → should read fine
    st = mgr.check_health("email", mode="private")
    assert st["health"] == "ok"
    assert st["last_sync"] is not None


def test_health_blocked_in_public_mode(mgr):
    st = mgr.check_health("email", mode="public")  # private-only → blocked
    assert st["health"] == "blocked"


def test_check_all_covers_kinds(mgr):
    results = mgr.check_all(mode="private")
    kinds = {r["connector"] for r in results}
    assert {"email", "calendar", "tasks"} <= kinds


def test_unknown_connector_health(mgr):
    st = mgr.check_health("doesnotexist")
    assert st["health"] == "unknown"
