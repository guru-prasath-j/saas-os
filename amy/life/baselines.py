"""LIFE AUTOPILOT — day-type-matched rolling baselines (hard rule 4).

Shared by L3's inference agents and L5's wellbeing index: "own baselines,
day-type-matched" means comparing a weekday to the trailing
AMY_LIFE_BASELINE_WEEKS weekday average (never weekends, never an
all-days blend — see docs/LIFE_AUTOPILOT.md test 10, the weekend-false-
positive regression), with grace (away/silent) days excluded from the
baseline sample entirely (hard rule 8).
"""
from __future__ import annotations

import datetime as _dt


def baseline_weeks() -> int:
    from .. import config
    try:
        return int(config._env("AMY_LIFE_BASELINE_WEEKS", "8"))
    except ValueError:
        return 8


def day_type_baseline(ctx, metric: str, day_type: str, as_of: str | None = None,
                      exclude_days: int = 0) -> dict | None:
    """Mean of life_metrics[metric] over the trailing baseline_weeks()
    weeks' days matching day_type ('weekday'|'weekend'), excluding grace
    days, NULL values, and the most recent `exclude_days` days (so the
    week being evaluated doesn't leak into its own baseline). None with
    fewer than 3 qualifying samples — never a baseline from thin air."""
    end = _dt.date.fromisoformat(as_of) if as_of else _dt.date.today()
    end -= _dt.timedelta(days=exclude_days)
    start = end - _dt.timedelta(weeks=baseline_weeks())
    rows = ctx.store.list_life_metrics(ctx.user_id, start.isoformat(), end.isoformat())
    vals = [r[metric] for r in rows
           if r.get("day_type") == day_type and not r.get("grace")
           and r.get(metric) is not None]
    if len(vals) < 3:
        return None
    return {"mean": sum(vals) / len(vals), "n": len(vals)}
