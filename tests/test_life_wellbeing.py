"""LIFE AUTOPILOT L5 — wellbeing index: adverse week -> exactly one line,
forbidden-phrase assertion, components inspectable, majority-grace week
-> no line. All sources are local SQLite fixtures."""
import datetime as _dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.collab import CollabDB
from amy.life import wellbeing as life_wellbeing

FORBIDDEN_PHRASES = [
    "you are stressed", "you're stressed", "you seem stressed",
    "you are burned out", "you're burned out", "burnout",
    "you are depressed", "you're depressed",
    "you are anxious", "you're anxious",
    "you have a mental health", "you need therapy", "you should see a doctor",
    "you are unwell", "diagnosis", "diagnosed with",
]


def _assert_no_forbidden_phrases(*texts: str) -> None:
    for text in texts:
        low = (text or "").lower()
        for phrase in FORBIDDEN_PHRASES:
            assert phrase not in low, f"forbidden phrase {phrase!r} found in: {text!r}"


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-wellbeing", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _seed_day(ctx, date, **fields):
    defaults = {"day_type": "weekday", "grace": False, "signal_counts": {}}
    defaults.update(fields)
    ctx.store.upsert_life_metrics(ctx.user_id, date, **defaults)


def _target_week_weekdays(week_monday: _dt.date) -> list[_dt.date]:
    return [week_monday + _dt.timedelta(days=i) for i in range(5)]


def test_adverse_week_produces_exactly_one_line(ctx):
    target_monday = life_wellbeing.last_completed_week()
    # 8-week weekday baseline ~480 min office, excluding the target+following week
    for w in range(2, 10):
        d = target_monday - _dt.timedelta(weeks=w - 1)
        for wd in range(5):
            day = d + _dt.timedelta(days=wd)
            _seed_day(ctx, day.isoformat(), office_minutes=480)
    # target week: elevated office time (+150min > 60 threshold)
    for day in _target_week_weekdays(target_monday):
        _seed_day(ctx, day.isoformat(), office_minutes=630)

    row = life_wellbeing.check_week(ctx)
    assert row is not None
    assert row["line_emitted"] is True
    assert "office_minutes" in row["components"]
    assert row["components"]["office_minutes"]["delta"] >= 60

    approvals = ctx.collab.conn.execute(
        "SELECT id, body, title FROM approvals WHERE source='life_wellbeing'").fetchall()
    assert len(approvals) == 1
    _assert_no_forbidden_phrases(approvals[0]["body"], approvals[0]["title"])
    assert "option" in approvals[0]["body"].lower()

    # idempotent recompute — no duplicate row, no duplicate proposal
    row2 = life_wellbeing.check_week(ctx)
    assert row2["week"] == row["week"]
    approvals2 = ctx.collab.conn.execute(
        "SELECT id FROM approvals WHERE source='life_wellbeing'").fetchall()
    assert len(approvals2) == 1
    weeks = ctx.collab.conn.execute(
        "SELECT COUNT(*) c FROM wellbeing_weekly WHERE uid=?", (ctx.user_id,)).fetchone()
    assert weeks["c"] == 1


def test_majority_grace_week_produces_no_line(ctx):
    target_monday = life_wellbeing.last_completed_week()
    for w in range(2, 10):
        d = target_monday - _dt.timedelta(weeks=w - 1)
        for wd in range(5):
            day = d + _dt.timedelta(days=wd)
            _seed_day(ctx, day.isoformat(), office_minutes=480)
    # target week: mostly grace days (away), only 2 non-grace days recorded
    days = [target_monday + _dt.timedelta(days=i) for i in range(7)]
    for i, day in enumerate(days):
        if i < 5:
            _seed_day(ctx, day.isoformat(), day_type="away", grace=True)
        else:
            _seed_day(ctx, day.isoformat(), office_minutes=700)   # would be adverse if judged

    row = life_wellbeing.check_week(ctx)
    assert row["line_emitted"] is False
    approvals = ctx.collab.conn.execute(
        "SELECT id FROM approvals WHERE source='life_wellbeing'").fetchall()
    assert len(approvals) == 0


def test_no_adverse_week_stores_components_but_no_line(ctx):
    target_monday = life_wellbeing.last_completed_week()
    for w in range(1, 10):
        d = target_monday - _dt.timedelta(weeks=w - 1)
        for wd in range(5):
            day = d + _dt.timedelta(days=wd)
            _seed_day(ctx, day.isoformat(), office_minutes=480)

    row = life_wellbeing.check_week(ctx)
    assert row["line_emitted"] is False
    assert "office_minutes" in row["components"]
    assert abs(row["components"]["office_minutes"]["delta"]) < 60


def test_components_inspectable_via_store_api(ctx):
    target_monday = life_wellbeing.last_completed_week()
    for w in range(1, 10):
        d = target_monday - _dt.timedelta(weeks=w - 1)
        for wd in range(5):
            day = d + _dt.timedelta(days=wd)
            _seed_day(ctx, day.isoformat(), office_minutes=480, gym_visits=1)

    life_wellbeing.check_week(ctx)
    stored = ctx.store.get_wellbeing_week(ctx.user_id, target_monday.isoformat())
    assert stored is not None
    comp = stored["components"]["office_minutes"]
    assert set(comp.keys()) >= {"value", "baseline_mean", "delta", "direction", "n"}

    listed = ctx.store.list_wellbeing_weeks(ctx.user_id, limit=5)
    assert any(w["week"] == target_monday.isoformat() for w in listed)


def test_sleep_and_gym_adverse_phrases(ctx):
    target_monday = life_wellbeing.last_completed_week()
    for w in range(2, 10):
        d = target_monday - _dt.timedelta(weeks=w - 1)
        for wd in range(5):
            day = d + _dt.timedelta(days=wd)
            _seed_day(ctx, day.isoformat(), sleep_estimate_min=420, gym_visits=1)
    for day in _target_week_weekdays(target_monday):
        _seed_day(ctx, day.isoformat(), sleep_estimate_min=350, gym_visits=0)

    row = life_wellbeing.check_week(ctx)
    assert row["line_emitted"] is True
    approvals = ctx.collab.conn.execute(
        "SELECT body FROM approvals WHERE source='life_wellbeing'").fetchone()
    body = approvals["body"].lower()
    assert "sleep" in body
    assert "gym" in body
    _assert_no_forbidden_phrases(approvals["body"])


def test_last_completed_week_is_previous_monday():
    as_of = _dt.date(2026, 7, 15)   # a Wednesday
    week = life_wellbeing.last_completed_week(as_of)
    assert week.weekday() == 0
    assert week < as_of - _dt.timedelta(days=7 - as_of.weekday())
    assert (as_of - week).days >= 7
