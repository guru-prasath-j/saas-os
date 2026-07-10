"""LIFE AUTOPILOT L2 — signal aggregator: day-typing, grace, idempotent
recompute, low-confidence-sleep NULLs, weekend classification, backfill
using only signals actually recorded. All sources are local SQLite fixtures
— no live network/LLM calls."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.collab import CollabDB
from amy.geo import GeoStore
from amy.life.aggregator import compute_day, infer_home_cell
from amy.life.backfill import backfill


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-life", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _insert_visit(ctx, place_id, entered_at, left_at=None):
    import uuid
    ctx.collab.conn.execute(
        "INSERT INTO geo_visits (id,place_id,entered_at,left_at) VALUES (?,?,?,?)",
        (uuid.uuid4().hex[:12], place_id, entered_at, left_at))
    ctx.collab.conn.commit()


def _insert_cell(ctx, cell, day, hits=1):
    ctx.collab.conn.execute(
        "INSERT INTO geo_cells (cell, day, hits) VALUES (?,?,?)"
        " ON CONFLICT(cell, day) DO UPDATE SET hits = hits + excluded.hits",
        (cell, day, hits))
    ctx.collab.conn.commit()


# A known weekday (2026-07-06 is a Monday) and weekend (2026-07-11, Saturday)
_WEEKDAY = "2026-07-06"
_WEEKEND = "2026-07-11"


def test_seeded_weekday_computes_correct_row(ctx):
    gs = GeoStore(ctx.collab)
    home_id = gs.add_place("Home", 12.90, 77.60, kind="home")
    office_id = gs.add_place("Office", 12.95, 77.65, kind="office")
    gym_id = gs.add_place("Gym", 12.93, 77.62, kind="gym")

    _insert_visit(ctx, home_id, f"{_WEEKDAY}T07:00:00", f"{_WEEKDAY}T08:00:00")
    _insert_visit(ctx, office_id, f"{_WEEKDAY}T08:45:00", f"{_WEEKDAY}T18:00:00")
    _insert_visit(ctx, gym_id, f"{_WEEKDAY}T19:00:00", f"{_WEEKDAY}T20:00:00")
    _insert_visit(ctx, home_id, f"{_WEEKDAY}T20:30:00", None)
    _insert_cell(ctx, "12.900,77.600", _WEEKDAY)

    fe = ctx.open_finance()
    try:
        fe.add_transaction(-450, "Food", "SWIGGY BANGALORE", date=_WEEKDAY)
        fe.add_transaction(-180, "Food", "STARBUCKS COFFEE", date=_WEEKDAY)
    finally:
        fe.close()

    row = compute_day(ctx, _WEEKDAY)
    ctx.store.upsert_life_metrics(ctx.user_id, _WEEKDAY, **row)

    assert row["day_type"] == "weekday"
    assert row["grace"] is False
    assert row["office_minutes"] == pytest.approx(555.0)   # 08:45 -> 18:00
    assert row["left_office_at"] == "18:00"
    assert row["gym_visits"] == 1
    assert row["home_arrival_at"] == "20:30"
    assert row["meals_out"] == 2         # swiggy + starbucks
    assert row["late_night_orders"] == 1  # swiggy only
    assert row["cafe_spend"] == pytest.approx(180.0)
    assert row["commute_out_minutes"] == pytest.approx(45.0)   # 08:00 -> 08:45
    assert row["commute_return_minutes"] == pytest.approx(150.0)  # 18:00 -> 20:30 (next home entry)

    stored = ctx.store.get_life_metrics(ctx.user_id, _WEEKDAY)
    assert stored is not None
    assert stored["day_type"] == "weekday"
    assert stored["gym_visits"] == 1


def test_idempotent_recompute_does_not_duplicate(ctx):
    gs = GeoStore(ctx.collab)
    gym_id = gs.add_place("Gym", 12.93, 77.62, kind="gym")
    _insert_visit(ctx, gym_id, f"{_WEEKDAY}T19:00:00", f"{_WEEKDAY}T20:00:00")
    _insert_cell(ctx, "12.900,77.600", _WEEKDAY)

    row1 = compute_day(ctx, _WEEKDAY)
    ctx.store.upsert_life_metrics(ctx.user_id, _WEEKDAY, **row1)
    first = ctx.store.get_life_metrics(ctx.user_id, _WEEKDAY)

    row2 = compute_day(ctx, _WEEKDAY)
    ctx.store.upsert_life_metrics(ctx.user_id, _WEEKDAY, **row2)
    second = ctx.store.get_life_metrics(ctx.user_id, _WEEKDAY)

    count = ctx.collab.conn.execute(
        "SELECT COUNT(*) AS c FROM life_metrics WHERE uid=? AND date=?",
        (ctx.user_id, _WEEKDAY)).fetchone()["c"]
    assert count == 1
    assert first["gym_visits"] == second["gym_visits"] == 1


def test_low_confidence_sleep_stays_null(ctx):
    # no home visits, no activity/capture timestamps -> nothing to infer from
    GeoStore(ctx.collab)   # ensure geo tables exist
    _insert_cell(ctx, "12.900,77.600", _WEEKDAY)
    row = compute_day(ctx, _WEEKDAY)
    assert row["sleep_window_start"] is None
    assert row["sleep_window_end"] is None
    assert row["sleep_estimate_min"] is None


def test_away_day_consecutive_zero_home_marks_grace(ctx):
    gs = GeoStore(ctx.collab)
    home_id = gs.add_place("Home", 12.90, 77.60, kind="home")
    # home visited 5 days before the target date, then nothing for 3 days
    _insert_visit(ctx, home_id, "2026-07-01T07:00:00", "2026-07-01T08:00:00")

    away_date = "2026-07-04"   # 3 days after the last home signal
    row = compute_day(ctx, away_date)
    assert row["day_type"] == "away"
    assert row["grace"] is True


def test_weekend_classified_correctly_without_flags(ctx):
    """L2-scoped slice of the weekend-false-positive regression: a Saturday
    with ordinary activity gets day_type='weekend' via calendar day-of-week,
    not 'silent' or misclassified — L5's day-type-matched baseline (which
    would compare this against a *weekend* baseline, not an all-days one)
    is out of scope until that part lands."""
    gs = GeoStore(ctx.collab)
    home_id = gs.add_place("Home", 12.90, 77.60, kind="home")
    _insert_visit(ctx, home_id, f"{_WEEKEND}T09:00:00", f"{_WEEKEND}T10:00:00")
    _insert_cell(ctx, "12.900,77.600", _WEEKEND)

    row = compute_day(ctx, _WEEKEND)
    assert row["day_type"] == "weekend"
    assert row["grace"] is False


def test_silent_day_zero_signals(ctx):
    gs = GeoStore(ctx.collab)
    gs.add_place("Home", 12.90, 77.60, kind="home")   # tagged but never visited
    row = compute_day(ctx, "2026-07-08")
    assert row["day_type"] in ("away", "silent")   # no home signal at all -> away takes precedence
    assert row["grace"] is True


def test_home_cell_fallback_infers_most_frequented_cell(ctx):
    gs = GeoStore(ctx.collab)
    _insert_cell(ctx, "12.900,77.600", "2026-07-01", hits=5)
    _insert_cell(ctx, "12.900,77.600", "2026-07-02", hits=5)
    _insert_cell(ctx, "12.910,77.610", "2026-07-01", hits=1)

    cell = infer_home_cell(gs, as_of="2026-07-06")
    assert cell == "12.900,77.600"


def test_backfill_only_populates_actually_recorded_signals(ctx):
    """Early days have transactions but no geo history at all — geo-derived
    fields must stay honestly NULL for those days while finance-derived
    fields populate, never retro-inferred."""
    fe = ctx.open_finance()
    try:
        fe.add_transaction(-300, "Food", "ZOMATO ORDER", date="2026-06-01")
    finally:
        fe.close()
    gs = GeoStore(ctx.collab)
    home_id = gs.add_place("Home", 12.90, 77.60, kind="home")
    _insert_visit(ctx, home_id, "2026-06-05T08:00:00", "2026-06-05T09:00:00")
    _insert_cell(ctx, "12.900,77.600", "2026-06-05")

    result = backfill(ctx, "2026-06-01", "2026-06-05")
    assert result["days_computed"] == 5

    early = ctx.store.get_life_metrics(ctx.user_id, "2026-06-01")
    assert early["meals_out"] == 1
    assert early["office_minutes"] is None   # no geo history that day
    assert early["home_arrival_at"] is None

    later = ctx.store.get_life_metrics(ctx.user_id, "2026-06-05")
    assert later["home_arrival_at"] == "08:00"


def test_backfill_rejects_inverted_range(ctx):
    with pytest.raises(ValueError):
        backfill(ctx, "2026-06-05", "2026-06-01")
