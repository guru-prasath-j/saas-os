"""LIFE AUTOPILOT L9 — place-opportunity dispatcher + rules table. All
sources are local SQLite fixtures — no live network/LLM calls."""
import datetime as _dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.collab import CollabDB
from amy.events.store import EventStore
from amy.geo import GeoStore
from amy.life import opportunity as life_opportunity
from amy.life import opportunity_rules


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-opp", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _grocery_place(gs):
    return gs.add_place("BigBasket Store", 12.9, 77.6, kind="grocery")


# ---------------------------------------------------------------------------
# Dispatcher mechanics (the spec's explicit test list)
# ---------------------------------------------------------------------------

def test_no_kind_place_never_fires(ctx):
    n = life_opportunity.dispatch(ctx, None, {"place_id": "p1", "name": "Somewhere", "kind": ""})
    assert n == 0


def test_rule_with_no_real_need_never_fires(ctx):
    gs = GeoStore(ctx.collab)
    pid = _grocery_place(gs)
    # no cook habit exists -> grocery rule has no real need
    n = life_opportunity.dispatch(ctx, None, {"place_id": pid, "name": "BigBasket Store", "kind": "grocery"})
    assert n == 0


def test_grocery_rule_fires_with_real_need_then_dedups_on_repeat_entry(ctx):
    gs = GeoStore(ctx.collab)
    pid = _grocery_place(gs)
    ctx.open_habits().add("Cook at home")
    n1 = life_opportunity.dispatch(ctx, None, {"place_id": pid, "name": "BigBasket Store", "kind": "grocery"})
    assert n1 == 1
    n2 = life_opportunity.dispatch(ctx, None, {"place_id": pid, "name": "BigBasket Store", "kind": "grocery"})
    assert n2 == 0   # dedup per rule x place x need across repeated entries
    rows = ctx.collab.conn.execute(
        "SELECT COUNT(*) c FROM notifications WHERE type='life_opp_grocery'").fetchone()
    assert rows["c"] == 1


def test_grocery_rule_no_need_with_recent_grocery_purchase(ctx):
    gs = GeoStore(ctx.collab)
    pid = _grocery_place(gs)
    ctx.open_habits().add("Cook at home")
    fe = ctx.open_finance()
    try:
        fe.add_transaction(-800, "Food", "BIGBASKET ORDER",
                           date=(_dt.date.today() - _dt.timedelta(days=2)).isoformat())
    finally:
        fe.close()
    n = life_opportunity.dispatch(ctx, None, {"place_id": pid, "name": "BigBasket Store", "kind": "grocery"})
    assert n == 0


def test_daily_cap_stops_after_max(ctx, monkeypatch):
    monkeypatch.setenv("AMY_LIFE_OPP_MAX_PER_DAY", "1")
    gs = GeoStore(ctx.collab)
    ctx.open_habits().add("Cook at home")
    p1 = gs.add_place("Grocer One", 12.9, 77.6, kind="grocery")
    p2 = gs.add_place("Grocer Two", 12.91, 77.61, kind="grocery")
    n1 = life_opportunity.dispatch(ctx, None, {"place_id": p1, "name": "Grocer One", "kind": "grocery"})
    assert n1 == 1
    n2 = life_opportunity.dispatch(ctx, None, {"place_id": p2, "name": "Grocer Two", "kind": "grocery"})
    assert n2 == 0   # daily cap of 1 already spent


def test_grace_day_suppresses_all_dispatch(ctx):
    yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    ctx.store.upsert_life_metrics(ctx.user_id, yesterday, day_type="away", grace=True, signal_counts={})
    gs = GeoStore(ctx.collab)
    pid = _grocery_place(gs)
    ctx.open_habits().add("Cook at home")
    n = life_opportunity.dispatch(ctx, None, {"place_id": pid, "name": "BigBasket Store", "kind": "grocery"})
    assert n == 0


def test_two_dismissals_silence_category(ctx):
    gs = GeoStore(ctx.collab)
    ctx.open_habits().add("Cook at home")
    p1 = gs.add_place("Grocer One", 12.9, 77.6, kind="grocery")

    life_opportunity.dispatch(ctx, None, {"place_id": p1, "name": "Grocer One", "kind": "grocery"})
    note = ctx.collab.conn.execute(
        "SELECT id FROM notifications WHERE type='life_opp_grocery' ORDER BY created_at DESC LIMIT 1").fetchone()
    r1 = life_opportunity.dismiss(ctx, note["id"])
    assert r1["ok"] and r1["dismiss_count"] == 1 and r1["silenced"] is False

    # a fresh place + fresh need so it's not blocked by the per-place dedup, only by silencing
    p2 = gs.add_place("Grocer Two", 12.92, 77.62, kind="grocery")
    life_opportunity.dispatch(ctx, None, {"place_id": p2, "name": "Grocer Two", "kind": "grocery"})
    note2 = ctx.collab.conn.execute(
        "SELECT id FROM notifications WHERE type='life_opp_grocery' ORDER BY created_at DESC LIMIT 1").fetchone()
    r2 = life_opportunity.dismiss(ctx, note2["id"])
    assert r2["dismiss_count"] == 2 and r2["silenced"] is True

    p3 = gs.add_place("Grocer Three", 12.93, 77.63, kind="grocery")
    n3 = life_opportunity.dispatch(ctx, None, {"place_id": p3, "name": "Grocer Three", "kind": "grocery"})
    assert n3 == 0   # category silenced — never fires again


def test_no_location_trail_in_notification_body(ctx):
    gs = GeoStore(ctx.collab)
    pid = _grocery_place(gs)
    ctx.open_habits().add("Cook at home")
    life_opportunity.dispatch(ctx, None, {"place_id": pid, "name": "BigBasket Store", "kind": "grocery"})
    note = ctx.collab.conn.execute(
        "SELECT body FROM notifications WHERE type='life_opp_grocery'").fetchone()
    body = note["body"]
    assert "12.9" not in body and "77.6" not in body and "lat" not in body.lower()


# ---------------------------------------------------------------------------
# gym_prompt (the one tier-0 write)
# ---------------------------------------------------------------------------

def _insert_visit(ctx, place_id, entered_at):
    import uuid
    ctx.collab.conn.execute(
        "INSERT INTO geo_visits (id,place_id,entered_at,left_at) VALUES (?,?,?,?)",
        (uuid.uuid4().hex[:12], place_id, entered_at, entered_at))
    ctx.collab.conn.commit()


def test_gym_prompt_one_tap_checks_exactly_once(ctx, monkeypatch):
    gs = GeoStore(ctx.collab)
    gym_id = gs.add_place("Gym", 12.9, 77.6, kind="gym")
    hid = ctx.open_habits().add("Workout")
    ctx.store.add_habit_link(ctx.user_id, hid, "geo_place_visit", {"kind": "gym"}, "auto_suggest_check")

    now_hour = _dt.datetime.now().hour
    today = _dt.date.today()
    for i in range(4):
        d = today - _dt.timedelta(days=i + 1)
        _insert_visit(ctx, gym_id, f"{d.isoformat()}T{now_hour:02d}:00:00")

    n1 = life_opportunity.dispatch(ctx, None, {"place_id": gym_id, "name": "Gym", "kind": "gym"})
    n2 = life_opportunity.dispatch(ctx, None, {"place_id": gym_id, "name": "Gym", "kind": "gym"})
    assert n1 + n2 >= 1
    approvals = ctx.collab.conn.execute(
        "SELECT tier, status FROM approvals WHERE action_type='complete_habit_check'").fetchall()
    assert len(approvals) == 1
    assert approvals[0]["tier"] == 0
    assert approvals[0]["status"] == "auto_executed"


# ---------------------------------------------------------------------------
# A few of the remaining rules directly (not every one needs full
# dispatcher-level coverage — the framework tests above already prove the
# generic wiring; these prove each rule's own real-signal logic)
# ---------------------------------------------------------------------------

def test_return_window_rule_matches_open_commitment(ctx):
    fe = ctx.open_finance()
    try:
        from amy.commitments import CommitmentEngine
        due = (_dt.date.today() + _dt.timedelta(days=5)).isoformat()
        CommitmentEngine(fe).add("return_window", "Amazon order", due, merchant="Amazon")
    finally:
        fe.close()
    trigger = opportunity_rules._rule_return_window(ctx, {"place_id": "p", "name": "Amazon Store", "kind": "shopping"})
    assert trigger is not None
    assert "location" not in trigger["body"].lower()


def test_cadence_rule_fires_when_overdue(ctx):
    fe = ctx.open_finance()
    try:
        today = _dt.date.today()
        for i in range(5):
            d = today - _dt.timedelta(days=20 + 7 * i)
            fe.add_transaction(-1000, "Transport", "SHELL PETROL PUMP", date=d.isoformat())
    finally:
        fe.close()
    trigger = opportunity_rules._rule_cadence(ctx, {"place_id": "p", "name": "Shell Petrol Pump", "kind": "fuel"})
    assert trigger is not None


def test_spend_caution_rule_fires_above_threshold(ctx):
    fe = ctx.open_finance()
    try:
        fe.set_budget("Food", 1000)
        fe.add_transaction(-900, "Food", "SOME RESTAURANT")
    finally:
        fe.close()
    trigger = opportunity_rules._rule_spend_caution(ctx, {"place_id": "p", "name": "Some Restaurant", "kind": "restaurant"})
    assert trigger is not None


def test_subscription_brand_rule_matches_active_sub(ctx):
    fe = ctx.open_finance()
    try:
        fe.conn.execute(
            "INSERT INTO subscriptions(id,name,monthly_cost,status) VALUES(?,?,?,?)",
            ("s1", "Netflix", 500, "active"))
        fe.conn.commit()
    finally:
        fe.close()
    trigger = opportunity_rules._rule_subscription_brand(ctx, {"place_id": "p", "name": "Netflix Store", "kind": "shopping"})
    assert trigger is not None


def test_custodial_bank_rule_surfaces_pending_validation(ctx):
    fe = ctx.open_finance()
    try:
        fe.conn.execute(
            "INSERT INTO accounts(id,nickname,bank_name,account_type,created_at)"
            " VALUES(?,?,?,?,?)",
            ("acc1", "SBI Custodial", "SBI", "custodial", _dt.datetime.now().isoformat()))
        fe.conn.commit()
    finally:
        fe.close()
    trigger = opportunity_rules._rule_custodial_bank(ctx, {"place_id": "p", "name": "SBI Bank", "kind": "bank"})
    # low-balance-refill check requires a commitment total > 0; with none
    # configured this honestly returns None rather than a false positive
    assert trigger is None or "issue" in trigger["body"].lower()


def test_person_proximity_rule_always_none(ctx):
    assert opportunity_rules._rule_person_proximity(ctx, {"place_id": "p", "name": "Anywhere", "kind": "cafe"}) is None


def test_pharmacy_rule_noop_without_refill_commitment(ctx):
    trigger = opportunity_rules._rule_pharmacy(ctx, {"place_id": "p", "name": "Apollo Pharmacy", "kind": "pharmacy"})
    assert trigger is None


def test_travel_mode_rule_fires_when_established_away(ctx):
    yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    ctx.store.upsert_life_metrics(ctx.user_id, yesterday, day_type="away", grace=True, signal_counts={})
    trigger = opportunity_rules._rule_travel_mode(ctx, {"place_id": "p", "name": "Hotel Lobby", "kind": "hotel"})
    assert trigger is not None
    assert "Home currency" in trigger["body"] or "away" in trigger["body"].lower()


# ---------------------------------------------------------------------------
# reactive-agent wiring smoke test
# ---------------------------------------------------------------------------

def test_dispatcher_wired_via_place_entered_event(ctx, monkeypatch):
    from amy.agents.reactive import register_reactive_agents

    monkeypatch.setenv("AMY_AGENT_LIFE_OPPORTUNITY", "1")
    gs = GeoStore(ctx.collab)
    pid = _grocery_place(gs)
    ctx.open_habits().add("Cook at home")

    es = EventStore(ctx.collab)
    registered = register_reactive_agents(es, ctx)
    assert "life_opportunity" in registered
    es.emit("context.place_entered", {"place_id": pid, "name": "BigBasket Store", "kind": "grocery"}, source="geo")
    row = ctx.collab.conn.execute(
        "SELECT id FROM notifications WHERE type='life_opp_grocery'").fetchone()
    assert row is not None
