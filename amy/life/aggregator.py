"""LIFE AUTOPILOT L2 — daily signal aggregator.

compute_day(ctx, date) builds one life_metrics row from whatever signals
are actually recorded that day (geo visits, transactions, captures,
activities, calendar) — never fabricates a value a source doesn't
support; unavailable metrics stay None (honest nulls, hard rule 7).

Known data-granularity constraints (see docs/AGENT_PLAN.md's LIFE
AUTOPILOT pre-flight findings for the full writeup):
  - geo_cells is (cell, day, hits) only — no time-of-day. Home-cell
    inference therefore can't restrict to night hours; it falls back to
    the single most-frequented cell overall over the baseline window.
  - transactions.date has no time-of-day. late_night_orders is a
    merchant-identity proxy (known food-delivery apps), not an
    hour-verified signal — documented, not fabricated.
  - office/commute/home-arrival/gym use geo_visits.entered_at/left_at,
    which DO carry full timestamps, so those are real durations when a
    place of that kind is tagged and visited.

Day typing + grace are computed HERE and consumed everywhere downstream
(L3/L4/L5/L9): away = >= AMY_LIFE_TRAVEL_GRACE_DAYS consecutive days with
no home signal at all; silent = zero signals across every source; else
weekday/weekend from the calendar day-of-week. away/silent both set
grace=True (out of baselines, pauses streaks, suppresses nudges).
"""
from __future__ import annotations

import datetime as _dt

_FOOD_DELIVERY_MERCHANT_TOKENS = (
    "swiggy", "zomato", "eatsure", "freshmenu", "box8", "faasos",
    "dominos", "mcdonalds", "mcd", "kfc", "pizza", "burger",
)
_CAFE_MERCHANT_TOKENS = (
    "cafe", "coffee", "starbucks", "chaayos", "chai point", "costa", "ccd",
)
_MEALS_OUT_MERCHANT_TOKENS = _FOOD_DELIVERY_MERCHANT_TOKENS + _CAFE_MERCHANT_TOKENS + (
    "restaurant", "dineout", "dhaba", "biryani", "hotel restaurant",
)


def _merchant_matches(merchant: str, tokens: tuple[str, ...]) -> bool:
    m = (merchant or "").lower()
    return any(t in m for t in tokens)


def _hhmm(iso_ts: str | None) -> str | None:
    if not iso_ts:
        return None
    try:
        return iso_ts[11:16] or None
    except Exception:
        return None


def _minutes_between(a_iso: str, b_iso: str) -> float | None:
    try:
        a = _dt.datetime.fromisoformat(a_iso)
        b = _dt.datetime.fromisoformat(b_iso)
    except Exception:
        return None
    return round((b - a).total_seconds() / 60.0, 1)


# ---------------------------------------------------------------------------
# Home-cell inference (L2 dual strategy: tagged place, else most-frequented
# cell over the baseline window — see the module docstring's granularity note)
# ---------------------------------------------------------------------------

def _baseline_weeks() -> int:
    from .. import config
    try:
        return int(config._env("AMY_LIFE_BASELINE_WEEKS", "8"))
    except ValueError:
        return 8


def infer_home_cell(gs, as_of: str | None = None) -> str | None:
    """Most-frequented geo_cells cell (by total hits) over the trailing
    baseline window. None with no cell data — never guesses."""
    end = _dt.date.fromisoformat(as_of) if as_of else _dt.date.today()
    start = end - _dt.timedelta(weeks=_baseline_weeks())
    rows = gs.db.conn.execute(
        "SELECT cell, SUM(hits) AS total FROM geo_cells WHERE day>=? AND day<=?"
        " GROUP BY cell ORDER BY total DESC LIMIT 1",
        (start.isoformat(), end.isoformat())).fetchall()
    return rows[0]["cell"] if rows else None


def _home_place_id(gs) -> str | None:
    for p in gs.list_places():
        if p.get("kind") == "home":
            return p["id"]
    return None


def _has_home_signal(gs, date: str, home_place_id: str | None, home_cell: str | None) -> bool:
    """True if there's evidence the user was home on `date`: a home-kind
    geo_visit that day, or (fallback) the inferred home cell appears in
    geo_cells for that day."""
    if home_place_id:
        row = gs.db.conn.execute(
            "SELECT 1 FROM geo_visits WHERE place_id=? AND substr(entered_at,1,10)=?"
            " LIMIT 1", (home_place_id, date)).fetchone()
        if row:
            return True
    if home_cell:
        row = gs.db.conn.execute(
            "SELECT 1 FROM geo_cells WHERE cell=? AND day=? LIMIT 1",
            (home_cell, date)).fetchone()
        if row:
            return True
    return False


def _consecutive_zero_home_days(gs, date: str, home_place_id: str | None,
                                home_cell: str | None, n: int) -> bool:
    """True if `date` and the (n-1) days before it all lack a home signal —
    the 'away' condition. n=1 degenerates to just checking `date` itself."""
    d = _dt.date.fromisoformat(date)
    for i in range(n):
        day = (d - _dt.timedelta(days=i)).isoformat()
        if _has_home_signal(gs, day, home_place_id, home_cell):
            return False
    return True


def _travel_grace_days() -> int:
    from .. import config
    try:
        return int(config._env("AMY_LIFE_TRAVEL_GRACE_DAYS", "2"))
    except ValueError:
        return 2


# ---------------------------------------------------------------------------
# Per-source signal extraction (each independently best-effort)
# ---------------------------------------------------------------------------

def _geo_signals(gs, date: str) -> dict:
    out = {"office_minutes": None, "commute_out_minutes": None,
          "commute_return_minutes": None, "left_office_at": None,
          "gym_visits": 0, "home_arrival_at": None, "_visit_count": 0}
    places = {p["id"]: p for p in gs.list_places()}
    rows = gs.db.conn.execute(
        "SELECT * FROM geo_visits WHERE substr(entered_at,1,10)=? ORDER BY entered_at",
        (date,)).fetchall()
    visits = [dict(r) for r in rows]
    out["_visit_count"] = len(visits)
    if not visits:
        return out

    def _kind_visits(kind: str) -> list[dict]:
        return [v for v in visits if places.get(v["place_id"], {}).get("kind") == kind]

    office_visits = _kind_visits("office")
    if office_visits:
        durations = [_minutes_between(v["entered_at"], v["left_at"])
                    for v in office_visits if v.get("left_at")]
        durations = [d for d in durations if d is not None]
        if durations:
            out["office_minutes"] = round(sum(durations), 1)
        left_times = [v["left_at"] for v in office_visits if v.get("left_at")]
        if left_times:
            out["left_office_at"] = _hhmm(max(left_times))

    gym_visits = _kind_visits("gym")
    out["gym_visits"] = len(gym_visits)

    home_visits = _kind_visits("home")
    if home_visits:
        out["home_arrival_at"] = _hhmm(max(v["entered_at"] for v in home_visits))

    # Commute: gap between a home departure and the first office arrival
    # that day (out), and between an office departure and the next home
    # arrival (return) — only when the gap is a plausible commute (<4h).
    if office_visits:
        first_office_in = min(v["entered_at"] for v in office_visits)
        home_left_before = [v["left_at"] for v in home_visits
                            if v.get("left_at") and v["left_at"] < first_office_in]
        if home_left_before:
            gap = _minutes_between(max(home_left_before), first_office_in)
            if gap is not None and 0 <= gap <= 240:
                out["commute_out_minutes"] = gap
        last_office_out = max((v["left_at"] for v in office_visits if v.get("left_at")), default=None)
        if last_office_out:
            home_in_after = [v["entered_at"] for v in home_visits
                             if v["entered_at"] > last_office_out]
            if home_in_after:
                gap = _minutes_between(last_office_out, min(home_in_after))
                if gap is not None and 0 <= gap <= 240:
                    out["commute_return_minutes"] = gap

    return out


def _finance_signals(fe, date: str) -> dict:
    txns = fe.list_transactions(limit=500, since=date, until=date)
    debits = [t for t in txns if (t.get("amount") or 0) < 0]
    meals_out = sum(1 for t in debits if _merchant_matches(t.get("merchant", ""), _MEALS_OUT_MERCHANT_TOKENS))
    late_night_orders = sum(1 for t in debits
                            if _merchant_matches(t.get("merchant", ""), _FOOD_DELIVERY_MERCHANT_TOKENS))
    cafe_spend = round(sum(abs(t["amount"]) for t in debits
                           if _merchant_matches(t.get("merchant", ""), _CAFE_MERCHANT_TOKENS)), 2)
    return {"meals_out": meals_out, "late_night_orders": late_night_orders,
           "cafe_spend": cafe_spend, "_txn_count": len(txns)}


def _capture_signals(ctx, date: str) -> dict:
    try:
        from .. import captures as captures_mod
        from ..saas import tenancy
        vault = tenancy.resolve_vault_dir(ctx.user_id)
        recs = captures_mod.captures_between(date, date, vault=vault)
    except Exception:
        recs = []
    return {"_capture_count": len(recs), "_capture_times": [r.get("created") for r in recs if r.get("created")]}


def _activity_signals(ctx, date: str) -> dict:
    rows = ctx.collab.conn.execute(
        "SELECT ts, kind FROM activities WHERE substr(ts,1,10)=?", (date,)).fetchall()
    activities = [dict(r) for r in rows]
    reading = sum(1 for a in activities if a["kind"] == "learning")
    return {"reading_minutes": 0.0, "_reading_events": reading,
           "_activity_count": len(activities),
           "_activity_times": [a["ts"] for a in activities]}


def _calendar_signals(ctx, date: str) -> dict:
    """Best-effort; a missing/failed calendar connection degrades to
    honest Nones (never fabricated). No generic past-date-range calendar
    helper exists in this codebase (meet_upcoming_meetings only looks
    forward) — building a real one is deferred; this stays None until
    L3's meeting-load agent needs it enough to justify that addition."""
    return {"meeting_count": None, "meeting_minutes": None, "focus_blocks": None}


# ---------------------------------------------------------------------------
# Sleep-window inference (conservative — geo home-arrival + activity-silence
# gap; NULL whenever confidence is low, per the AskUserQuestion decision)
# ---------------------------------------------------------------------------

def _infer_sleep_window(geo: dict, activity_times: list[str], capture_times: list[str],
                        home_arrival_at: str | None) -> tuple[str | None, str | None, float | None]:
    if not home_arrival_at:
        return None, None, None
    all_times = sorted(t for t in (activity_times + capture_times) if t)
    if not all_times:
        return None, None, None
    # last activity of the (evening) day, first activity of the next morning
    late = [t for t in all_times if _hhmm(t) and _hhmm(t) >= home_arrival_at]
    if not late:
        return None, None, None
    sleep_start = max(late)
    early_next = [t for t in all_times if t > sleep_start and _hhmm(t) and _hhmm(t) <= "10:00"]
    if not early_next:
        return None, None, None
    sleep_end = min(early_next)
    minutes = _minutes_between(sleep_start, sleep_end)
    if minutes is None or not (120 <= minutes <= 720):
        # implausible gap (too short to be sleep, or too long — probably a
        # multi-day activity silence, not a real sleep window) -> NULL
        return None, None, None
    return _hhmm(sleep_start), _hhmm(sleep_end), minutes


# ---------------------------------------------------------------------------
# Day typing + grace
# ---------------------------------------------------------------------------

def _classify_day(gs, date: str, signal_total: int) -> tuple[str, bool]:
    home_place_id = _home_place_id(gs)
    home_cell = infer_home_cell(gs, as_of=date)
    n = _travel_grace_days()
    if _consecutive_zero_home_days(gs, date, home_place_id, home_cell, n):
        return "away", True
    if signal_total == 0:
        return "silent", True
    weekday = _dt.date.fromisoformat(date).weekday()  # 0=Mon .. 6=Sun
    return ("weekend" if weekday >= 5 else "weekday"), False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def compute_day(ctx, date: str) -> dict:
    """Builds one life_metrics row for `date` from every signal source,
    each independently best-effort. Idempotent — callers upsert the
    result via AutomationStore.upsert_life_metrics."""
    gs = _geo_store(ctx)
    geo = _geo_signals(gs, date)

    fe = ctx.open_finance()
    try:
        fin = _finance_signals(fe, date)
    finally:
        fe.close()

    cap = _capture_signals(ctx, date)
    act = _activity_signals(ctx, date)
    cal = _calendar_signals(ctx, date)

    sleep_start, sleep_end, sleep_min = _infer_sleep_window(
        geo, act["_activity_times"], cap["_capture_times"], geo["home_arrival_at"])

    signal_counts = {
        "geo_visits": geo["_visit_count"], "transactions": fin["_txn_count"],
        "captures": cap["_capture_count"], "activities": act["_activity_count"],
    }
    signal_total = sum(signal_counts.values())
    day_type, grace = _classify_day(gs, date, signal_total)

    return {
        "office_minutes": geo["office_minutes"],
        "commute_out_minutes": geo["commute_out_minutes"],
        "commute_return_minutes": geo["commute_return_minutes"],
        "left_office_at": geo["left_office_at"],
        "gym_visits": geo["gym_visits"],
        "home_arrival_at": geo["home_arrival_at"],
        "sleep_window_start": sleep_start,
        "sleep_window_end": sleep_end,
        "sleep_estimate_min": sleep_min,
        "meals_out": fin["meals_out"],
        "late_night_orders": fin["late_night_orders"],
        "cafe_spend": fin["cafe_spend"],
        "meeting_count": cal["meeting_count"],
        "meeting_minutes": cal["meeting_minutes"],
        "focus_blocks": cal["focus_blocks"],
        "reading_minutes": act["reading_minutes"],
        "late_night_activity_min": None,   # L3 (meeting-load/sleep agents) territory
        "meal_captures": 0,                # L8 adds real meal classification
        "meal_calorie_est": None,
        "day_type": day_type,
        "grace": grace,
        "signal_counts": signal_counts,
    }


def _geo_store(ctx):
    from ..geo import GeoStore
    return GeoStore(ctx.collab)
