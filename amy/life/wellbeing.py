"""LIFE AUTOPILOT L5 — wellbeing index.

Weekly, terminal-advisory (nothing downstream keys on it — L6's Life
Review reads it for the monthly narrative, but no agent branches on it).
Components are plain metric deltas against day-type-matched baselines
(hard rule 4) — NO inferred emotional/medical state is ever computed or
stored, satisfying hard rule 1 by construction: there is no code path
that could produce a diagnostic claim, because nothing here represents
mental/physical state, only raw metric numbers.

A majority-grace week (fewer than 4 non-grace days) produces NO line —
hard rule 8. Otherwise, at most ONE briefing line
(AMY_LIFE_WELLBEING_MAX_LINES=1, naturally satisfied since this computes
once per week) — observation + option phrasing, reusing L3's propose()
framework (dedup/resuggest-window/drift-silence) for the "declining
remembered" requirement, since the anti-nag semantics are identical.
"""
from __future__ import annotations

import datetime as _dt

from .baselines import day_type_baseline

_COMPONENTS = ("office_minutes", "sleep_estimate_min", "gym_visits")
_MIN_JUDGED_DAYS = 4   # fewer non-grace days than this -> majority-grace week, no line

_OFFICE_ADVERSE_MIN = 60      # +60 min/day vs baseline
_SLEEP_ADVERSE_MAX = -30      # -30 min/day vs baseline


def _week_monday(d: _dt.date) -> _dt.date:
    return d - _dt.timedelta(days=d.weekday())


def last_completed_week(as_of: _dt.date | None = None) -> _dt.date:
    """Monday of the most recently fully-completed week (never the
    in-progress current week — a partial week isn't a fair baseline
    comparison)."""
    today = as_of or _dt.date.today()
    this_monday = _week_monday(today)
    return this_monday - _dt.timedelta(weeks=1)


def _component_delta(ctx, metric: str, week_rows: list[dict]) -> dict | None:
    judged = [r for r in week_rows if not r.get("grace") and r.get(metric) is not None]
    if not judged:
        return None
    by_type: dict[str, list[float]] = {"weekday": [], "weekend": []}
    for r in judged:
        if r.get("day_type") in by_type:
            by_type[r["day_type"]].append(r[metric])

    weighted_deltas = []
    total_n = 0
    for day_type, vals in by_type.items():
        if not vals:
            continue
        baseline = day_type_baseline(ctx, metric, day_type, exclude_days=7)
        if not baseline:
            continue
        mean_val = sum(vals) / len(vals)
        weighted_deltas.append((mean_val - baseline["mean"]) * len(vals))
        total_n += len(vals)
    if not total_n:
        return None
    delta = sum(weighted_deltas) / total_n
    value = sum(v for vals in by_type.values() for v in vals) / max(
        1, sum(len(vals) for vals in by_type.values()))
    baseline_mean = value - delta
    direction = "up" if delta > 0.01 else ("down" if delta < -0.01 else "flat")
    return {"value": round(value, 1), "baseline_mean": round(baseline_mean, 1),
           "delta": round(delta, 1), "direction": direction, "n": total_n}


def _adverse_phrases(components: dict) -> list[str]:
    phrases = []
    office = components.get("office_minutes")
    if office and office["delta"] >= _OFFICE_ADVERSE_MIN:
        phrases.append(f"office +{office['delta']:.0f}min/day")
    sleep = components.get("sleep_estimate_min")
    if sleep and sleep["delta"] <= _SLEEP_ADVERSE_MAX:
        phrases.append(f"sleep {sleep['delta']:.0f}min/day")
    gym = components.get("gym_visits")
    if gym and gym["value"] == 0 and gym["baseline_mean"] > 0:
        phrases.append("no gym visits")
    return phrases


def check_week(ctx, week_start: str | None = None) -> dict | None:
    """Computes (or returns the already-computed) wellbeing row for the
    given week (default: last_completed_week()). Idempotent per week."""
    week_date = _dt.date.fromisoformat(week_start) if week_start else last_completed_week()
    week_key = week_date.isoformat()

    existing = ctx.store.get_wellbeing_week(ctx.user_id, week_key)
    if existing:
        return existing

    end = week_date + _dt.timedelta(days=6)
    week_rows = ctx.store.list_life_metrics(ctx.user_id, week_key, end.isoformat())
    non_grace = [r for r in week_rows if not r.get("grace")]
    majority_grace = len(non_grace) < _MIN_JUDGED_DAYS

    components = {}
    for metric in _COMPONENTS:
        c = _component_delta(ctx, metric, week_rows)
        if c:
            components[metric] = c
    adverse_count = sum(1 for c in components.values() if c["direction"] in ("up", "down")
                        and abs(c["delta"]) > 0)
    index_delta = None
    if components:
        index_delta = round(sum(c["delta"] for c in components.values()) / len(components), 1)

    line_emitted = False
    if not majority_grace and week_rows:
        phrases = _adverse_phrases(components)
        if phrases:
            line_emitted = _emit_line(ctx, week_key, phrases)

    ctx.store.upsert_wellbeing_week(ctx.user_id, week_key, components, index_delta, line_emitted)

    try:
        ctx.events().emit(
            "life.wellbeing_week_computed",
            {"week": week_key, "line_emitted": line_emitted,
             "components": list(components.keys())},
            source="wellbeing")
    except Exception:
        pass

    return ctx.store.get_wellbeing_week(ctx.user_id, week_key)


def _emit_line(ctx, week_key: str, phrases: list[str]) -> bool:
    """ONE observation + option line, reusing L3's propose() framework
    for the 'declining remembered' resuggest-window/dedup semantics —
    the anti-nag requirements are identical to L3's, so this is a
    deliberate reuse, not a parallel mechanism."""
    from .inference import propose

    line = (", ".join(phrases) +
           " — a 10-min wind-down habit is one option; want it proposed?")
    result = propose(
        ctx, "wellbeing", f"week_{week_key}",
        title="Weekly wellbeing check-in",
        body=line,
        action_type="propose_habit",
        payload={"title": "Wind-down routine", "frequency": "daily",
                "link": {"signal_type": "sleep_window_met", "signal_params": {},
                        "mode": "auto_suggest_check"}},
        reasoning=f"Adverse wellbeing signals for week of {week_key}: {'; '.join(phrases)}.")
    return result is not None
