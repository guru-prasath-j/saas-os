"""CONTEXT_PLAN C4–C7 — cadences, pattern tasks, relationship nudges,
universal-inbox executor, preference drift."""
import datetime as _dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.agents.reactive import register_reactive_agents
from amy.automation import build_ctx
from amy.automation.drift import _signals, preference_drift
from amy.automation.executors import execute
from amy.collab import CollabDB
from amy.events.store import EventStore
from amy.geo import GeoStore
from amy.patterns import (cadence, merchant_cadences, pattern_tasks,
                          person_cadences, relationship_nudges)


def _d(n: int) -> str:
    return (_dt.date.today() - _dt.timedelta(days=n)).isoformat()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    cdb = CollabDB(str(tmp_path / "collab.db"))
    ctx = build_ctx("u-pat", "t@example.com", cdb, tmp_path, llm_router=None)
    yield ctx, cdb
    cdb.close()


# --- C4: cadence math -------------------------------------------------------

def test_cadence_regular_weekly():
    c = cadence([_d(28), _d(21), _d(14), _d(7)])
    assert c and c["gap_days"] == 7 and c["next_due"] == _d(0)


def test_cadence_rejects_irregular_and_sparse():
    assert cadence([_d(60), _d(41), _d(9), _d(2)]) is None   # chaotic gaps
    assert cadence([_d(14), _d(7), _d(0)]) is None            # only 3 dates


def test_pattern_tasks_proposes_prefilled_task(env):
    ctx, cdb = env
    fe = ctx.open_finance()
    try:
        for n in (28, 21, 14, 7):   # weekly groceries, next due today
            fe.add_transaction(-900, "Food", "RATNADEEP SUPER MARKET",
                               date=_d(n))
    finally:
        fe.close()
    GeoStore(cdb).add_place("Ratnadeep", 13.0827, 80.2707, kind="grocery")

    out = pattern_tasks(ctx)
    assert out["proposed"] == 1
    prop = [a for a in ctx.store.list_approvals(status="pending")
            if a["action_type"] == "add_task"]
    assert len(prop) == 1
    payload = prop[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["place_tag"] == "grocery"     # prefilled from saved place

    assert pattern_tasks(ctx)["duplicates"] == 1   # same cycle → dedup

    # approving creates the task; the errand agent then matches it on arrival
    res = execute(ctx, "add_task", payload)
    es = EventStore(cdb)
    register_reactive_agents(es, ctx)
    es.emit("context.place_entered",
            {"place_id": "p1", "name": "Ratnadeep", "kind": "grocery"},
            source="test")
    notifs = ctx.notify_store().list()
    assert any(n["type"] == "errand_reminder" and "RATNADEEP" in n["body"]
               for n in notifs)


def test_pattern_tasks_skips_when_open_task_exists(env):
    ctx, cdb = env
    fe = ctx.open_finance()
    try:
        for n in (28, 21, 14, 7):
            fe.add_transaction(-900, "Food", "RATNADEEP", date=_d(n))
    finally:
        fe.close()
    cdb.conn.execute(
        "INSERT INTO tasks (id,goal_id,title,done,created_at,place_tag)"
        " VALUES ('t1','','Ratnadeep grocery run',0,datetime('now'),'')")
    cdb.conn.commit()
    assert pattern_tasks(ctx)["proposed"] == 0


# --- C5: relationship nudges --------------------------------------------------

def test_relationship_nudge_when_rhythm_breaks(env):
    ctx, _ = env
    fe = ctx.open_finance()
    try:
        # 21-day rhythm (tolerance 6), all inside the 120d lookback; last one
        # 29 days ago → 8 days past due = 2 past tolerance → inside the window
        for n in (113, 92, 71, 50, 29):
            fe.add_transaction(-5000, "Transfer", "SATHISH APPA", date=_d(n))
    finally:
        fe.close()
    out = relationship_nudges(ctx)
    assert out["nudged"] == 1
    notifs = ctx.notify_store().list()
    assert any(n["type"] == "relationship_nudge" and "SATHISH" in n["title"]
               for n in notifs)


def test_relationship_nudge_quiet_when_on_rhythm(env):
    ctx, _ = env
    fe = ctx.open_finance()
    try:
        for n in (90, 60, 30, 1):    # last one yesterday — healthy rhythm
            fe.add_transaction(-5000, "Transfer", "AMMA", date=_d(n))
    finally:
        fe.close()
    assert relationship_nudges(ctx)["nudged"] == 0


# --- C6: universal inbox executor --------------------------------------------

def test_external_draft_executor_is_ack_only(env):
    ctx, _ = env
    out = execute(ctx, "external_draft", {"draft_id": "wa_123",
                                          "text": "reply to electrician"})
    assert out == {"acknowledged": True, "draft": "wa_123"}


# --- C7: preference drift ------------------------------------------------------

def test_drift_signals():
    rows = (
        [{"action_type": "add_subscription", "source": "subscription_agent",
          "status": "rejected"}] * 4
        + [{"action_type": "add_subscription", "source": "subscription_agent",
            "status": "executed"}]
        + [{"action_type": "import_statement", "source": "gmail_ingest",
            "status": "executed"}] * 6
        + [{"action_type": "add_place", "source": "place_learning",
            "status": "expired"}] * 3
    )
    kinds = {s["kind"] for s in _signals(rows)}
    assert kinds == {"always_reject", "always_approve", "ignored"}


def test_preference_drift_job(env):
    ctx, _ = env
    for i in range(5):
        ctx.store.create_approval(tier=2, action_type="add_subscription",
                                  title=f"s{i}", source="subscription_agent",
                                  status="rejected")
    ctx.store.create_approval(tier=2, action_type="add_subscription",
                              title="s-ok", source="subscription_agent",
                              status="executed")
    out = preference_drift(ctx)
    assert out["signals"] >= 1
    notifs = ctx.notify_store().list()
    assert any(n["type"] == "preference_drift" for n in notifs)
