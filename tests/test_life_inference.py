"""LIFE AUTOPILOT L3 — nine inference agents + the shared propose()
framework (dedup, post-rejection resuggest window, drift-pruning silence).
All sources are local SQLite fixtures — no live network/LLM calls."""
import datetime as _dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.collab import CollabDB
from amy.geo import GeoStore
from amy.life import inference as life_inference


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-inference", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _seed_day(ctx, date, **fields):
    defaults = {"day_type": "weekday", "grace": False, "signal_counts": {}}
    defaults.update(fields)
    ctx.store.upsert_life_metrics(ctx.user_id, date, **defaults)


# ---------------------------------------------------------------------------
# Shared framework
# ---------------------------------------------------------------------------

def test_propose_dedups_same_pattern(ctx):
    r1 = life_inference.propose(ctx, "test_agent", "pk", "T", "B", "propose_goal",
                                {"title": "X", "domain": "life"}, "reasoning")
    assert r1 is not None and r1["status"] == "pending"
    r2 = life_inference.propose(ctx, "test_agent", "pk", "T", "B", "propose_goal",
                                {"title": "X", "domain": "life"}, "reasoning")
    assert r2 is None   # already pending -> dedup


def test_propose_respects_resuggest_window_after_rejection(ctx):
    dedup_key = "life_test_agent_pk2"
    ctx.collab.conn.execute(
        "INSERT INTO approvals(id,created_at,tier,action_type,title,payload,status,source,dedup_key)"
        " VALUES('a1',?,2,'propose_goal','t','{}','rejected','life_test_agent',?)",
        (_dt.datetime.now(_dt.timezone.utc).isoformat(), dedup_key))
    ctx.collab.conn.commit()
    r = life_inference.propose(ctx, "test_agent", "pk2", "T", "B", "propose_goal",
                               {"title": "X"}, "reasoning")
    assert r is None   # rejected recently -> still inside the resuggest window

    # backdate the rejection beyond the window -> now allowed
    old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=30)).isoformat()
    ctx.collab.conn.execute("UPDATE approvals SET created_at=? WHERE id='a1'", (old,))
    ctx.collab.conn.commit()
    r2 = life_inference.propose(ctx, "test_agent", "pk2", "T", "B", "propose_goal",
                                {"title": "X"}, "reasoning")
    assert r2 is not None and r2["status"] == "pending"


def test_propose_silenced_by_drift_pruning(ctx):
    for i in range(4):
        ctx.collab.conn.execute(
            "INSERT INTO approvals(id,created_at,tier,action_type,title,payload,status,source,dedup_key)"
            " VALUES(?,?,2,'propose_goal','t','{}','rejected','life_drifty',?)",
            (f"d{i}", _dt.datetime.now(_dt.timezone.utc).isoformat(), f"life_drifty_pk{i}"))
    ctx.collab.conn.commit()
    r = life_inference.propose(ctx, "drifty", "brand_new_pattern", "T", "B", "propose_goal",
                               {"title": "X"}, "reasoning")
    assert r is None   # always_reject signal for (propose_goal, life_drifty) silences everything


# ---------------------------------------------------------------------------
# 1. commute
# ---------------------------------------------------------------------------

def test_commute_office_minutes_above_baseline_proposes_leave_by(ctx):
    today = _dt.date.today()
    # 8-week weekday baseline ~ 480 min/day, excluding the most recent 7 days
    for w in range(1, 9):
        d = today - _dt.timedelta(weeks=w)
        if d.weekday() >= 5:
            d -= _dt.timedelta(days=d.weekday() - 4)
        _seed_day(ctx, d.isoformat(), office_minutes=480, left_office_at="18:00")
    # this week: elevated office time on weekdays
    for i in range(5):
        d = today - _dt.timedelta(days=i)
        if d.weekday() >= 5:
            continue
        _seed_day(ctx, d.isoformat(), office_minutes=620, left_office_at="20:30")

    results = life_inference.commute_check(ctx)
    assert any(r for r in results)
    row = ctx.collab.conn.execute(
        "SELECT payload FROM approvals WHERE action_type='propose_habit'"
        " AND dedup_key='life_commute_leave_by'").fetchone()
    assert row is not None


def test_commute_late_arrivals_proposes_goal(ctx):
    today = _dt.date.today()
    for i in range(5):
        d = today - _dt.timedelta(days=i)
        _seed_day(ctx, d.isoformat(), home_arrival_at="21:30")
    results = life_inference.commute_check(ctx)
    row = ctx.collab.conn.execute(
        "SELECT id FROM approvals WHERE dedup_key='life_commute_late_arrivals'").fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# 2. meals
# ---------------------------------------------------------------------------

def test_meals_late_night_orders_proposes_cook_habit(ctx):
    today = _dt.date.today()
    for i in range(4):
        _seed_day(ctx, (today - _dt.timedelta(days=i)).isoformat(), late_night_orders=1)
    results = life_inference.meals_check(ctx)
    row = ctx.collab.conn.execute(
        "SELECT payload FROM approvals WHERE dedup_key='life_meals_cook_habit'").fetchone()
    assert row is not None
    import json
    payload = json.loads(row["payload"])
    assert payload["link"]["signal_type"] == "txn_absence"


def test_meals_cafe_cadence_proposes_home_brew_with_savings(ctx):
    fe = ctx.open_finance()
    try:
        today = _dt.date.today()
        for i in range(5):
            d = today - _dt.timedelta(days=7 * i)
            fe.add_transaction(-150, "Food", "STARBUCKS COFFEE", date=d.isoformat())
    finally:
        fe.close()
    results = life_inference.meals_check(ctx)
    row = ctx.collab.conn.execute(
        "SELECT body FROM approvals WHERE dedup_key='life_meals_home_brew'").fetchone()
    assert row is not None
    assert "save" in row["body"].lower() or "month" in row["body"].lower()


# ---------------------------------------------------------------------------
# 3. sleep
# ---------------------------------------------------------------------------

def test_sleep_short_streak_proposes_wind_down_and_goal(ctx):
    today = _dt.date.today()
    for i in range(6):
        _seed_day(ctx, (today - _dt.timedelta(days=i)).isoformat(), sleep_estimate_min=300)
    results = life_inference.sleep_check(ctx)
    habit_row = ctx.collab.conn.execute(
        "SELECT id FROM approvals WHERE dedup_key='life_sleep_wind_down'").fetchone()
    goal_row = ctx.collab.conn.execute(
        "SELECT id FROM approvals WHERE dedup_key='life_sleep_improve_goal'").fetchone()
    assert habit_row is not None
    assert goal_row is not None


def test_sleep_post_midnight_streak_proposes_device_down(ctx):
    today = _dt.date.today()
    for i in range(6):
        _seed_day(ctx, (today - _dt.timedelta(days=i)).isoformat(), sleep_window_start="01:30")
    life_inference.sleep_check(ctx)
    row = ctx.collab.conn.execute(
        "SELECT id FROM approvals WHERE dedup_key='life_sleep_device_down'").fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# 4. activity
# ---------------------------------------------------------------------------

def _insert_visit(ctx, place_id, entered_at):
    import uuid
    ctx.collab.conn.execute(
        "INSERT INTO geo_visits (id,place_id,entered_at,left_at) VALUES (?,?,?,?)",
        (uuid.uuid4().hex[:12], place_id, entered_at, entered_at))
    ctx.collab.conn.commit()


def test_activity_gym_cadence_proposes_workout_habit(ctx):
    gs = GeoStore(ctx.collab)
    gym_id = gs.add_place("Gym", 12.9, 77.6, kind="gym")
    today = _dt.date.today()
    for i in range(5):
        d = today - _dt.timedelta(days=7 * i)
        _insert_visit(ctx, gym_id, f"{d.isoformat()}T18:00:00")
    results = life_inference.activity_check(ctx)
    row = ctx.collab.conn.execute(
        "SELECT payload FROM approvals WHERE dedup_key='life_activity_gym_habit'").fetchone()
    assert row is not None


def test_activity_absence_after_regularity_sends_gentle_nudge_not_proposal(ctx):
    gs = GeoStore(ctx.collab)
    gym_id = gs.add_place("Gym", 12.9, 77.6, kind="gym")
    today = _dt.date.today()
    for i in range(1, 5):
        d = today - _dt.timedelta(days=7 * i + 12)   # regular visits, none in the last 12 days
        _insert_visit(ctx, gym_id, f"{d.isoformat()}T18:00:00")
    ctx.store.add_habit_link(ctx.user_id, "h1", "geo_place_visit", {"kind": "gym"}, "auto_complete")

    life_inference.activity_check(ctx)
    approval_row = ctx.collab.conn.execute(
        "SELECT id FROM approvals WHERE dedup_key='life_activity_gym_habit'").fetchone()
    assert approval_row is None   # already linked -> no NEW habit proposal
    notif_row = ctx.collab.conn.execute(
        "SELECT id FROM notifications WHERE type='life_activity_nudge'").fetchone()
    assert notif_row is not None


# ---------------------------------------------------------------------------
# 5. reading
# ---------------------------------------------------------------------------

def test_reading_cadence_proposes_read_habit(ctx):
    today = _dt.date.today()
    for i in range(5):
        d = today - _dt.timedelta(days=7 * i)
        ctx.collab.conn.execute(
            "INSERT INTO activities (ts,kind,detail,domain) VALUES (?,?,?,?)",
            (f"{d.isoformat()}T09:00:00", "learning", "Article", None))
    ctx.collab.conn.commit()
    life_inference.reading_check(ctx)
    row = ctx.collab.conn.execute(
        "SELECT payload FROM approvals WHERE dedup_key='life_reading_read_habit'").fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# 6. meeting-load
# ---------------------------------------------------------------------------

def test_meeting_load_weekend_office_streak_proposes_protect_weekend(ctx):
    today = _dt.date.today()
    days_since_saturday = (today.weekday() - 5) % 7
    saturdays = [today - _dt.timedelta(days=days_since_saturday + 7 * w) for w in range(4)]
    for d in saturdays[:3]:
        _seed_day(ctx, d.isoformat(), day_type="weekend", office_minutes=90)
    life_inference.meeting_load_check(ctx)
    row = ctx.collab.conn.execute(
        "SELECT id FROM approvals WHERE dedup_key='life_meeting_load_protect_weekend'").fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# 7. admin
# ---------------------------------------------------------------------------

def test_admin_insurance_renewal_proposes_goal(ctx):
    fe = ctx.open_finance()
    try:
        due = (_dt.date.today() + _dt.timedelta(days=10)).isoformat()
        fe.conn.execute(
            "INSERT INTO subscriptions(id,name,renewal_date,status) VALUES(?,?,?,?)",
            ("sub1", "Health Insurance", due, "active"))
        fe.conn.commit()
    finally:
        fe.close()
    life_inference.admin_check(ctx)
    row = ctx.collab.conn.execute(
        "SELECT id FROM approvals WHERE dedup_key='life_admin_insurance_sub1'").fetchone()
    assert row is not None


def test_admin_jurisdiction_deadline_proposes_goal(ctx, monkeypatch):
    due = (_dt.date.today() + _dt.timedelta(days=5)).isoformat()
    monkeypatch.setattr("amy.jurisdictions.load_pack", lambda jid: {"id": jid})
    monkeypatch.setattr(
        "amy.jurisdictions.upcoming_deadlines",
        lambda pack, horizon_days=90: [
            {"jurisdiction": pack["id"], "name": "ITR Filing", "kind": "compliance",
             "date": due, "days_away": 5}])
    ctx._extras["jurisdictions"] = ["india"]
    life_inference.admin_check(ctx)
    row = ctx.collab.conn.execute(
        "SELECT id FROM approvals WHERE dedup_key='life_admin_deadline_india_ITR Filing'").fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# 8. seasonal
# ---------------------------------------------------------------------------

def test_seasonal_pack_note_proposes_before_period(ctx, monkeypatch):
    # fixed reference date so 'today' and 'lookahead' land in different
    # months deterministically (Jan 20 + 14 days -> February)
    as_of = "2026-01-20"
    lookahead_month = 2
    monkeypatch.setattr(
        "amy.jurisdictions.load_pack",
        lambda jid: {"id": jid, "seasonal_notes": [
            {"months": [lookahead_month], "note": "Test seasonal period."}]})
    ctx._extras["jurisdictions"] = ["india"]
    life_inference.seasonal_check(ctx, as_of=as_of)
    rows = ctx.collab.conn.execute(
        "SELECT id FROM approvals WHERE action_type='propose_goal'"
        " AND dedup_key LIKE 'life_seasonal_%'").fetchall()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# 9. social
# ---------------------------------------------------------------------------

def test_social_broken_rhythm_proposes_call_habit(ctx):
    fe = ctx.open_finance()
    try:
        today = _dt.date.today()
        for i in range(1, 6):
            d = today - _dt.timedelta(days=7 * i + 20)   # regular, but nothing recently
            fe.add_transaction(-500, "Transfer", "Mom", date=d.isoformat())
    finally:
        fe.close()
    life_inference.social_check(ctx)
    rows = ctx.collab.conn.execute(
        "SELECT id FROM approvals WHERE dedup_key LIKE 'life_social_call_%'").fetchall()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# run_all orchestration
# ---------------------------------------------------------------------------

def test_run_all_respects_per_agent_kill_switch(ctx, monkeypatch):
    monkeypatch.setenv("AMY_AGENT_LIFE_COMMUTE", "0")
    out = life_inference.run_all(ctx)
    assert out["commute"] == {"skipped": "disabled"}
    assert "meals" in out   # other agents still ran (or errored/proposed, but present)
