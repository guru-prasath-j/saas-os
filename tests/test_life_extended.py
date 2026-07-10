"""LIFE AUTOPILOT L8 — extended signals: meal captures, commitments
crossover, health_data wearable stub. All sources mocked — no live
network/LLM calls."""
import datetime as _dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.collab import CollabDB
from amy.life import aggregator as life_aggregator
from amy.life import commitments_life, health_data, meal_captures


class StubLLM:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def generate(self, system, prompt, context="", sensitive=False, fast=False):
        self.calls.append({"sensitive": sensitive})
        body = self._response if isinstance(self._response, str) else json.dumps(self._response)
        return (body, "scripted")


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-l8", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


# ---------------------------------------------------------------------------
# Meal captures
# ---------------------------------------------------------------------------

def test_meal_classification_increments_with_estimate(ctx):
    rec = {"caption": "A plate of biryani", "ocr": "", "tags": ["food", "lunch"]}
    llm = StubLLM({"is_meal": True, "calorie_estimate": 650})
    result = meal_captures.classify_capture(rec, llm)
    assert result == {"is_meal": True, "calorie_estimate": 650.0}
    assert llm.calls[0]["sensitive"] is True


def test_meal_classification_low_confidence_stays_null(ctx):
    rec = {"caption": "A plate of biryani", "ocr": "", "tags": ["food"]}
    llm = StubLLM({"is_meal": True, "calorie_estimate": None})
    result = meal_captures.classify_capture(rec, llm)
    assert result == {"is_meal": True, "calorie_estimate": None}


def test_meal_classification_not_a_meal_returns_none(ctx):
    rec = {"caption": "A sunset over the hills", "ocr": "", "tags": ["nature"]}
    llm = StubLLM({"is_meal": False, "calorie_estimate": None})
    assert meal_captures.classify_capture(rec, llm) is None


def test_day_meal_signals_aggregates_multiple_captures(ctx, monkeypatch):
    recs = [
        {"caption": "Breakfast", "ocr": "", "tags": ["food"]},
        {"caption": "Random photo", "ocr": "", "tags": []},
        {"caption": "Dinner", "ocr": "", "tags": ["food"]},
    ]
    monkeypatch.setattr("amy.captures.captures_between", lambda start, end, vault=None: recs)

    responses = iter([
        {"is_meal": True, "calorie_estimate": 400},
        {"is_meal": False, "calorie_estimate": None},
        {"is_meal": True, "calorie_estimate": 700},
    ])

    class MultiStub:
        def generate(self, system, prompt, context="", sensitive=False, fast=False):
            return (json.dumps(next(responses)), "scripted")

    out = meal_captures.day_meal_signals(ctx, "2026-07-06", MultiStub())
    assert out == {"meal_captures": 2, "meal_calorie_est": 1100.0}


def test_capture_meal_link_completes_at_day_close(ctx):
    from amy.life.habits import evaluate_day_close

    hid = ctx.open_habits().add("Log meals")
    ctx.store.add_habit_link(ctx.user_id, hid, "capture_meal", {"min_captures": 1}, "auto_suggest_check")
    ctx.store.upsert_life_metrics(ctx.user_id, "2026-07-06", day_type="weekday", grace=False,
                                  meal_captures=2, signal_counts={})
    n = evaluate_day_close(ctx, "2026-07-06")
    assert n == 1


# ---------------------------------------------------------------------------
# health_data wearable stub
# ---------------------------------------------------------------------------

def test_health_data_no_connector_is_honestly_unavailable(ctx):
    sleep = health_data.fetch_device_day(ctx, "2026-07-06")
    activity = health_data.fetch_device_activity(ctx, "2026-07-06")
    assert sleep == {"available": False}
    assert activity == {"available": False}


def test_health_data_available_when_connector_registered(ctx, monkeypatch):
    monkeypatch.setattr(
        "amy.connectors.mcp_call.call_mcp_tool",
        lambda uid, store, source, candidates, args, target_style="none":
            {"result": {"sleep_start": "23:15", "sleep_end": "06:45", "duration_min": 450}})
    out = health_data.fetch_device_day(ctx, "2026-07-06")
    assert out["available"] is True
    assert out["sleep_window_start"] == "23:15"
    assert out["sleep_estimate_min"] == 450.0


def test_aggregator_prefers_device_sleep_when_available(ctx, monkeypatch):
    monkeypatch.setattr(
        "amy.life.aggregator._apply_device_sleep",
        lambda ctx, date, s, e, m: ("22:00", "06:00", 480.0, "device"))
    row = life_aggregator.compute_day(ctx, "2026-07-06")
    assert row["sleep_provenance"] == "device"
    assert row["sleep_estimate_min"] == 480.0


def test_steps_workout_link_types_complete_at_day_close(ctx, monkeypatch):
    from amy.life.habits import evaluate_day_close

    monkeypatch.setattr(
        "amy.life.health_data.fetch_device_activity",
        lambda ctx, date: {"available": True, "steps": 8000, "workouts": 1})
    hid = ctx.open_habits().add("Hit step goal")
    ctx.store.add_habit_link(ctx.user_id, hid, "steps", {"min_steps": 5000}, "auto_complete")
    ctx.store.upsert_life_metrics(ctx.user_id, "2026-07-06", day_type="weekday", grace=False, signal_counts={})
    n = evaluate_day_close(ctx, "2026-07-06")
    assert n == 1


# ---------------------------------------------------------------------------
# Commitments crossover
# ---------------------------------------------------------------------------

def test_pharmacy_cadence_proposes_refill_commitment(ctx):
    fe = ctx.open_finance()
    try:
        today = _dt.date.today()
        for i in range(5):
            d = today - _dt.timedelta(days=10 * i)
            fe.add_transaction(-300, "Health", "APOLLO PHARMACY", date=d.isoformat())
    finally:
        fe.close()

    results = commitments_life.pharmacy_refill_check(ctx)
    assert len(results) == 1
    row = ctx.collab.conn.execute(
        "SELECT payload FROM approvals WHERE action_type='add_commitment'"
        " AND dedup_key LIKE 'life_commitments_crossover_refill_%'").fetchone()
    assert row is not None
    payload = json.loads(row["payload"])
    assert payload["kind"] == "custom"
    assert "Apollo" in payload["title"] or "APOLLO" in payload["title"]


def test_pharmacy_refill_not_duplicated_when_commitment_already_open(ctx):
    fe = ctx.open_finance()
    try:
        today = _dt.date.today()
        for i in range(5):
            d = today - _dt.timedelta(days=10 * i)
            fe.add_transaction(-300, "Health", "APOLLO PHARMACY", date=d.isoformat())
        from amy.commitments import CommitmentEngine
        CommitmentEngine(fe).add("custom", "Refill: APOLLO PHARMACY",
                                 (today + _dt.timedelta(days=10)).isoformat())
    finally:
        fe.close()
    results = commitments_life.pharmacy_refill_check(ctx)
    assert results == []


def test_annual_checkup_proposes_once_per_year(ctx):
    results = commitments_life.annual_checkup_check(ctx)
    assert len(results) == 1
    results2 = commitments_life.annual_checkup_check(ctx)
    assert results2 == []   # resuggest-window dedup blocks a same-year repeat


def test_annual_checkup_skips_when_already_scheduled(ctx):
    fe = ctx.open_finance()
    try:
        from amy.commitments import CommitmentEngine
        year = _dt.date.today().year
        CommitmentEngine(fe).add("custom", "Annual health checkup",
                                 _dt.date(year, 11, 1).isoformat())
    finally:
        fe.close()
    results = commitments_life.annual_checkup_check(ctx)
    assert results == []


# ---------------------------------------------------------------------------
# L8 -> L9 integration: pharmacy refill commitment feeds L9's pharmacy rule
# ---------------------------------------------------------------------------

def test_l8_refill_commitment_feeds_l9_pharmacy_rule(ctx):
    from amy.life import opportunity_rules

    fe = ctx.open_finance()
    try:
        today = _dt.date.today()
        for i in range(5):
            d = today - _dt.timedelta(days=10 * i)
            fe.add_transaction(-300, "Health", "APOLLO PHARMACY", date=d.isoformat())
    finally:
        fe.close()

    proposed = commitments_life.pharmacy_refill_check(ctx)
    assert len(proposed) == 1
    approval_id = proposed[0]["approval_id"]

    from amy.automation.executors import approve
    approve(ctx, approval_id)

    trigger = opportunity_rules._rule_pharmacy(
        ctx, {"place_id": "p", "name": "Apollo Pharmacy", "kind": "pharmacy"})
    assert trigger is not None
    assert "refill" in trigger["title"].lower()
