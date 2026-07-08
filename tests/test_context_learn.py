"""CONTEXT_PLAN C2 — place learning from spending + spend-aware geofencing."""
import datetime as _dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.agents.reactive import register_reactive_agents
from amy.automation import build_ctx
from amy.automation.executors import execute
from amy.collab import CollabDB
from amy.events.store import EventStore
from amy.geo import GeoStore
from amy.geo.learn import place_learning, suggest_places

HOME = (13.0827, 80.2707)
NEAR_30M = (13.08297, 80.2707)
SHOP = (13.10, 80.28)               # far from HOME, stable cell
FAR = (13.20, 80.40)


def _days_ago(n: int) -> str:
    return (_dt.date.today() - _dt.timedelta(days=n)).isoformat()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    cdb = CollabDB(str(tmp_path / "collab.db"))
    ctx = build_ctx("u-learn", "t@example.com", cdb, tmp_path, llm_router=None)
    es = EventStore(cdb)
    register_reactive_agents(es, ctx)
    yield ctx, es, cdb
    cdb.close()


def _seed_correlation(ctx, cdb, merchant="RATNADEEP", days=(2, 9, 16)):
    """Same merchant charge + same unmatched cell on the same N days."""
    gs = GeoStore(cdb)
    fe = ctx.open_finance()
    try:
        for n in days:
            fe.add_transaction(-800, "Food", merchant, date=_days_ago(n))
            gs.ingest_location(*SHOP, ts=f"{_days_ago(n)}T10:00:00+00:00")
    finally:
        fe.close()
    return gs


def test_cells_only_track_unmatched_fixes(env):
    ctx, _, cdb = env
    gs = GeoStore(cdb)
    gs.add_place("Home", *HOME, kind="home", radius_m=150)
    gs.ingest_location(*NEAR_30M)     # inside Home → no cell
    gs.ingest_location(*FAR)          # unmatched → cell
    cells = gs.cell_days(min_days=1)
    assert len(cells) == 1
    day = next(iter(cells.values()))
    assert list(day)[0] == _dt.date.today().isoformat()


def test_suggest_places_correlates_merchant_and_cell(env):
    ctx, _, cdb = env
    gs = _seed_correlation(ctx, cdb)
    fe = ctx.open_finance()
    try:
        sugg = suggest_places(fe, gs)
    finally:
        fe.close()
    assert len(sugg) == 1
    s = sugg[0]
    assert s["merchant"] == "RATNADEEP"
    assert s["kind"] == "grocery"           # Food category → grocery kind
    assert len(s["overlap_days"]) == 3
    assert abs(s["lat"] - SHOP[0]) < 0.001 and abs(s["lon"] - SHOP[1]) < 0.001


def test_suggest_places_skips_spot_already_saved(env):
    ctx, _, cdb = env
    gs = _seed_correlation(ctx, cdb)
    gs.add_place("Ratnadeep", *SHOP, kind="grocery", radius_m=150)
    fe = ctx.open_finance()
    try:
        assert suggest_places(fe, gs) == []
    finally:
        fe.close()


def test_suggest_places_needs_recurrence(env):
    """One-off merchant (< 3 txn days) never becomes a geofence proposal."""
    ctx, _, cdb = env
    gs = _seed_correlation(ctx, cdb, merchant="ONE-OFF STORE", days=(2, 9))
    fe = ctx.open_finance()
    try:
        assert suggest_places(fe, gs) == []
    finally:
        fe.close()


def test_place_learning_job_proposes_then_dedups_then_executes(env):
    ctx, _, cdb = env
    _seed_correlation(ctx, cdb)

    out1 = place_learning(ctx)
    assert out1["proposed"] == 1
    pending = ctx.store.list_approvals(status="pending")
    prop = [a for a in pending if a["action_type"] == "add_place"]
    assert len(prop) == 1
    assert "RATNADEEP" in prop[0]["title"]
    assert prop[0]["reasoning"]

    out2 = place_learning(ctx)                    # same suggestion → dedup
    assert out2["proposed"] == 0 and out2["duplicates"] == 1

    payload = prop[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    res = execute(ctx, "add_place", payload)      # approval → executor
    gs = GeoStore(cdb)
    saved = [p for p in gs.list_places() if p["id"] == res["place_id"]]
    assert saved and saved[0]["source"] == "learned"
    assert saved[0]["kind"] == "grocery"


def test_spend_caution_on_entering_matching_place(env):
    ctx, es, cdb = env
    fe = ctx.open_finance()
    try:
        fe.set_budget("Food", 1000)
        fe.add_transaction(-950, "Food", "SWIGGY")   # 95% consumed
    finally:
        fe.close()
    ev = {"place_id": "p9", "name": "Ratnadeep", "kind": "grocery"}
    es.emit("context.place_entered", ev, source="test")

    notifs = [n for n in ctx.notify_store().list() if n["type"] == "spend_caution"]
    assert len(notifs) == 1
    assert "Food" in notifs[0]["body"] and "95%" in notifs[0]["body"]

    es.emit("context.place_entered", ev, source="test")   # same day → dedup
    notifs2 = [n for n in ctx.notify_store().list() if n["type"] == "spend_caution"]
    assert len(notifs2) == 1


def test_no_spend_caution_below_threshold_or_unrelated_kind(env):
    ctx, es, cdb = env
    fe = ctx.open_finance()
    try:
        fe.set_budget("Food", 1000)
        fe.add_transaction(-200, "Food", "SWIGGY")       # 20% — fine
        fe.set_budget("Shopping", 1000)
        fe.add_transaction(-950, "Shopping", "AMAZON")   # 95% but wrong kind
    finally:
        fe.close()
    es.emit("context.place_entered",
            {"place_id": "p1", "name": "Ratnadeep", "kind": "grocery"},
            source="test")
    assert not [n for n in ctx.notify_store().list()
                if n["type"] == "spend_caution"]
