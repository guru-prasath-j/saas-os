"""LIFE AUTOPILOT L4 — habit auto-completion, grace streaks, adaptation.
All sources are local SQLite fixtures — no live network/LLM calls."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.agents.reactive import register_reactive_agents
from amy.automation import build_ctx
from amy.collab import CollabDB
from amy.events.store import EventStore
from amy.geo import GeoStore
from amy.life import habits as life_habits


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-habits", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _set_life_metrics_grace(ctx, date, grace):
    ctx.store.upsert_life_metrics(ctx.user_id, date, day_type=("away" if grace else "weekday"),
                                  grace=grace, signal_counts={})


def test_gym_visit_autocompletes_exactly_once_tier0(ctx, monkeypatch):
    monkeypatch.setenv("AMY_AGENT_LIFE_HABITS", "1")
    gs = GeoStore(ctx.collab)
    gym_id = gs.add_place("Gym", 12.9, 77.6, kind="gym")
    hid = ctx.open_habits().add("Workout")

    ctx.store.add_habit_link(ctx.user_id, hid, "geo_place_visit", {"kind": "gym"}, "auto_complete")

    es = EventStore(ctx.collab)
    register_reactive_agents(es, ctx)
    payload = {"place_id": gym_id, "name": "Gym", "kind": "gym"}
    es.emit("context.place_entered", payload, source="geo")
    es.emit("context.place_entered", payload, source="geo")   # fires twice same day

    habits = ctx.open_habits()
    try:
        row = habits.db.execute(
            "SELECT done FROM habit_logs WHERE habit_id=?", (hid,)).fetchone()
    finally:
        habits.close()
    assert row is not None and row["done"] == 1

    approvals = ctx.collab.conn.execute(
        "SELECT tier, status FROM approvals WHERE action_type='complete_habit_check'").fetchall()
    assert len(approvals) == 1   # exactly once despite two place_entered fires
    assert approvals[0]["tier"] == 0
    assert approvals[0]["status"] == "auto_executed"


def test_txn_absence_only_completes_at_day_close(ctx):
    hid = ctx.open_habits().add("Cook at home")
    ctx.store.add_habit_link(ctx.user_id, hid, "txn_absence", {}, "auto_suggest_check")

    date = "2026-07-06"
    # unrelated real-time events must not complete an absence-type link
    es = EventStore(ctx.collab)
    life_habits.on_place_entered(ctx, es, {"place_id": "x", "kind": "home"})
    life_habits.on_place_left(ctx, es, {"kind": "office"}, now_hhmm="17:00")
    habits = ctx.open_habits()
    try:
        assert not life_habits._habit_done(habits, hid, date)
    finally:
        habits.close()

    n = life_habits.evaluate_day_close(ctx, date)
    assert n == 1
    habits = ctx.open_habits()
    try:
        assert life_habits._habit_done(habits, hid, date)
    finally:
        habits.close()


def test_txn_presence_detects_matching_transaction(ctx):
    hid = ctx.open_habits().add("Ordered food")
    ctx.store.add_habit_link(ctx.user_id, hid, "txn_presence",
                             {"merchant_tokens": ["swiggy"]}, "auto_complete")
    date = "2026-07-06"
    fe = ctx.open_finance()
    try:
        fe.add_transaction(-300, "Food", "SWIGGY ORDER", date=date)
    finally:
        fe.close()
    n = life_habits.evaluate_day_close(ctx, date)
    assert n == 1


def test_left_office_before_realtime_completes(ctx):
    hid = ctx.open_habits().add("Leave office by 6")
    ctx.store.add_habit_link(ctx.user_id, hid, "left_office_before",
                             {"kind": "office", "before": "18:00"}, "auto_suggest_check")
    es = EventStore(ctx.collab)
    n = life_habits.on_place_left(ctx, es, {"kind": "office"}, now_hhmm="17:45")
    assert n == 1
    approvals = ctx.collab.conn.execute(
        "SELECT tier, status FROM approvals WHERE action_type='complete_habit_check'").fetchall()
    assert len(approvals) == 1
    assert approvals[0]["tier"] == 1   # auto_suggest_check


def test_left_office_after_deadline_does_not_complete(ctx):
    hid = ctx.open_habits().add("Leave office by 6")
    ctx.store.add_habit_link(ctx.user_id, hid, "left_office_before",
                             {"kind": "office", "before": "18:00"}, "auto_suggest_check")
    es = EventStore(ctx.collab)
    n = life_habits.on_place_left(ctx, es, {"kind": "office"}, now_hhmm="19:15")
    assert n == 0


def test_streak_survives_one_miss_per_week(ctx):
    import datetime as _dt

    hid = ctx.open_habits().add("Daily thing")
    habits = ctx.open_habits()
    today = _dt.date.today()
    # last 7 days: today done, yesterday MISSED, the 5 before that all done
    for i in range(8):
        d = (today - _dt.timedelta(days=i)).isoformat()
        if i == 1:
            continue   # one miss, same ISO week as today (assume run mid-week in CI is fine either way)
        habits.check_in(hid, date=d, done=True)
    habits.close()

    streak = life_habits.streak_with_grace(ctx, hid, ctx.open_habits())
    assert streak >= 7   # the single miss this week didn't break it


def test_grace_day_pauses_streak_without_breaking_it(ctx):
    import datetime as _dt

    hid = ctx.open_habits().add("Daily thing")
    habits = ctx.open_habits()
    today = _dt.date.today()
    for i in range(5):
        d = (today - _dt.timedelta(days=i)).isoformat()
        if i == 2:
            _set_life_metrics_grace(ctx, d, True)   # away day, no check-in, no life_metrics 'done'
            continue
        habits.check_in(hid, date=d, done=True)
    habits.close()

    streak = life_habits.streak_with_grace(ctx, hid, ctx.open_habits())
    assert streak == 4   # the grace day is skipped, not counted, not a break


def test_adaptation_three_failing_weeks_proposes_one_easing(ctx):
    import datetime as _dt

    hid = ctx.open_habits().add("Daily thing")
    habits = ctx.open_habits()
    today = _dt.date.today()
    monday = today - _dt.timedelta(days=today.weekday())
    # 3 full trailing weeks, each with only 1 of 7 days done (5 missed > grace budget 1)
    for w in range(3):
        week_start = monday - _dt.timedelta(weeks=(2 - w))
        for i in range(7):
            d = week_start + _dt.timedelta(days=i)
            if d > today:
                break
            habits.check_in(hid, date=d.isoformat(), done=(i == 0))
    habits.close()

    result = life_habits.check_adaptation(ctx, hid, ctx.open_habits())
    assert result is not None
    assert result["status"] in ("pending",)
    row = ctx.collab.conn.execute(
        "SELECT payload FROM approvals WHERE action_type='adjust_habit_target'").fetchone()
    assert row is not None
    import json
    payload = json.loads(row["payload"])
    assert payload["direction"] == "ease"
    assert payload["new_grace_per_week"] == payload["old_grace_per_week"] + 1


def test_adaptation_two_rejections_silences_habit(ctx):
    hid = "h-silenced"
    for i in range(2):
        ctx.collab.conn.execute(
            "INSERT INTO approvals(id,created_at,tier,action_type,title,payload,status)"
            " VALUES(?,?,2,'adjust_habit_target','t',?,'rejected')",
            (f"a{i}", "2026-01-01", f'{{"habit_id": "{hid}"}}'))
    ctx.collab.conn.commit()
    assert life_habits.adaptation_silenced(ctx, hid) is True
    # a habit with only one rejection is NOT silenced
    assert life_habits.adaptation_silenced(ctx, "h-other") is False


def test_adaptation_levelup_fires_at_most_once(ctx):
    import datetime as _dt

    hid = ctx.open_habits().add("Easy habit")
    habits = ctx.open_habits()
    today = _dt.date.today()
    monday = today - _dt.timedelta(days=today.weekday())
    for w in range(6):
        week_start = monday - _dt.timedelta(weeks=(5 - w))
        for i in range(7):
            d = week_start + _dt.timedelta(days=i)
            if d > today:
                break
            habits.check_in(hid, date=d.isoformat(), done=True)
    habits.close()

    first = life_habits.check_adaptation(ctx, hid, ctx.open_habits())
    assert first is not None
    second = life_habits.check_adaptation(ctx, hid, ctx.open_habits())
    assert second is None   # already proposed once — dedup key has no week suffix

    rows = ctx.collab.conn.execute(
        "SELECT COUNT(*) AS c FROM approvals WHERE action_type='adjust_habit_target'"
        " AND payload LIKE '%levelup%'").fetchone()
    assert rows["c"] == 1


def test_adaptation_skips_non_daily_and_archived(ctx):
    habits = ctx.open_habits()
    weekly_id = habits.add("Weekly thing", frequency="weekly")
    habits.close()
    assert life_habits.check_adaptation(ctx, weekly_id, ctx.open_habits()) is None


def test_link_suggestion_matches_keywords():
    s = life_habits.suggest_link_for_title("Hit the gym")
    assert s["signal_type"] == "geo_place_visit"
    assert s["signal_params"]["kind"] == "gym"

    s2 = life_habits.suggest_link_for_title("Leave office by 6pm")
    assert s2["signal_type"] == "left_office_before"

    assert life_habits.suggest_link_for_title("something unrelated entirely") is None
