"""OL-4 — sync (reconcile + deltas) and replay."""
import os, tempfile
import pytest
from amy.collab.db import CollabDB
from amy.events.store import EventStore
from amy.operational import StateStore, SyncService, ReplayService, ConnectorManager
from amy.operational.models import EntityState


@pytest.fixture
def ctx():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    d = CollabDB(p); es = EventStore(d); ss = StateStore(d)
    yield d, es, ss
    d.close(); os.unlink(p)


def test_reconcile_add_update_remove(ctx):
    d, es, ss = ctx
    sync = SyncService(ss, es)
    r1 = sync.reconcile("github", "repo",
                        [EntityState("github:repo:a", "repo", "github", "A", {"prs": 1})])
    assert r1["added"] == 1
    r2 = sync.reconcile("github", "repo",
                        [EntityState("github:repo:a", "repo", "github", "A", {"prs": 2})])
    assert r2["updated"] == 1
    r3 = sync.reconcile("github", "repo", [], remove_missing=True)
    assert r3["removed"] == 1
    assert ss.list_entities(source="github") == []


def test_reconcile_emits_events(ctx):
    d, es, ss = ctx
    SyncService(ss, es).reconcile("github", "repo",
                                  [EntityState("x", "repo", "github", "X", {"v": 1})])
    assert len(es.recent("github.entity_added")) == 1


def test_unchanged_emits_nothing(ctx):
    d, es, ss = ctx
    sync = SyncService(ss, es)
    e = EntityState("x", "repo", "github", "X", {"v": 1})
    sync.reconcile("github", "repo", [e])
    before = len(es.recent("github.entity_updated"))
    sync.reconcile("github", "repo", [EntityState("x", "repo", "github", "X", {"v": 1})])
    assert len(es.recent("github.entity_updated")) == before  # no change → no event


def test_sync_connector_pulls_and_reconciles(ctx):
    d, es, ss = ctx
    cdir = tempfile.mkdtemp()
    cm = ConnectorManager(ss, connector_dir=cdir)
    res = SyncService(ss, es, connector_manager=cm).sync_connector("email", mode="private")
    assert "added" in res
    assert ss.get_connector_state("email")["last_sync"] is not None


def test_replay_filters_and_dispatches(ctx):
    d, es, ss = ctx
    es.emit("a.x", {"n": 1}); es.emit("b.y", {"n": 2}); es.emit("a.z", {"n": 3})
    got = []
    rep = ReplayService(d).replay(lambda ev: got.append(ev["type"]), types=["a.x", "a.z"])
    assert rep["dispatched"] == 2
    assert set(got) == {"a.x", "a.z"}


def test_replay_handler_error_isolated(ctx):
    d, es, ss = ctx
    es.emit("a.x", {}); es.emit("a.y", {})
    def bad(ev):
        raise ValueError("boom")
    rep = ReplayService(d).replay(bad)
    assert rep["errors"] == 2 and rep["dispatched"] == 0
