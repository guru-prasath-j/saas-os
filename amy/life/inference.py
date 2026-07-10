"""LIFE AUTOPILOT L3 — nine inference agents.

Each agent below is a plain function `xxx_check(ctx) -> list[dict]` called
weekly by the life_inference_scan job (no push event exists for "a
behavioral pattern changed" — same no-op-subscription + scan-job idiom as
meeting_prep_scan). Every proposal goes through propose() — the shared
cross-cutting framework every one of the nine needs identically per the
binding spec ("each: kill switch, weekly-rollup driven, tier-2 evidence-
mandatory proposals, dedup per pattern key"):

  - dedup per pattern key: submit_action's own dedup_key blocks a repeat
    while pending/executed.
  - "declined respects resuggest window": a REJECTED proposal is NOT
    blocked by dedup_key alone (create_approval's dedup only checks
    pending/executed/auto_executed) — propose() additionally checks the
    approvals table for a rejection within AMY_LIFE_RESUGGEST_DAYS.
  - "drift-pruning silences permanently": reuses amy/automation/drift.py's
    existing (action_type, source) signal computation rather than a new
    pruning table — each agent proposes under source=f"life_{agent}", so
    an always_reject signal for that (action_type, source) pair silences
    every future proposal of that action_type from that agent.

propose_habit/propose_goal (amy/automation/executors.py) are how these
agents wire into L4: a habit proposal can carry a `link` that creates the
matching habit_links row atomically on approval — e.g. the commute
agent's "leave by 6" proposal creates the habit AND its
left_office_before link in one approve.
"""
from __future__ import annotations

import datetime as _dt
import statistics

from .baselines import baseline_weeks, day_type_baseline

_CAFE_TOKENS = ("cafe", "coffee", "starbucks", "chaayos", "chai point", "costa", "ccd")
_FOOD_DELIVERY_TOKENS = ("swiggy", "zomato", "eatsure", "freshmenu", "box8", "faasos",
                        "dominos", "mcdonalds", "mcd", "kfc", "pizza", "burger")
_LATE_NIGHT_WEEKLY_THRESHOLD = 3
_SHORT_SLEEP_STREAK_DAYS = 5
_POST_MIDNIGHT_STREAK_DAYS = 5
_GYM_ABSENCE_DAYS = 10
_HEAVY_MEETING_WEEKS = 2
_WEEKEND_OFFICE_STREAK = 3
_DEADLINE_HORIZON_DAYS = 30
_SEASONAL_LOOKAHEAD_DAYS = 14


def _merchant_matches(merchant: str, tokens: tuple[str, ...]) -> bool:
    m = (merchant or "").lower()
    return any(t in m for t in tokens)


def _trailing_rows(ctx, days: int, day_type: str | None = None) -> list[dict]:
    end = _dt.date.today()
    start = end - _dt.timedelta(days=days)
    rows = ctx.store.list_life_metrics(ctx.user_id, start.isoformat(), end.isoformat())
    if day_type:
        rows = [r for r in rows if r.get("day_type") == day_type]
    return rows


def _median_time(times: list[str]) -> str | None:
    times = [t for t in times if t]
    if not times:
        return None
    minutes = sorted(int(t[:2]) * 60 + int(t[3:5]) for t in times)
    m = int(statistics.median(minutes))
    return f"{m // 60:02d}:{m % 60:02d}"


# ---------------------------------------------------------------------------
# Shared proposal framework
# ---------------------------------------------------------------------------

def _resuggest_days() -> int:
    from .. import config
    try:
        return int(config._env("AMY_LIFE_RESUGGEST_DAYS", "21"))
    except ValueError:
        return 21


def _drift_silenced(ctx, agent: str, action_type: str) -> bool:
    from ..automation.drift import _signals
    rows = [dict(r) for r in ctx.collab.conn.execute(
        "SELECT action_type, source, status FROM approvals"
        " WHERE source=? AND action_type=?"
        " AND status IN ('executed','auto_executed','rejected','expired')",
        (f"life_{agent}", action_type)).fetchall()]
    return any(s["kind"] == "always_reject" for s in _signals(rows))


def _resuggest_ok(ctx, dedup_key: str) -> bool:
    row = ctx.collab.conn.execute(
        "SELECT created_at FROM approvals WHERE dedup_key=? AND status='rejected'"
        " ORDER BY created_at DESC LIMIT 1", (dedup_key,)).fetchone()
    if not row:
        return True
    try:
        last = _dt.datetime.fromisoformat(row["created_at"])
    except Exception:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=_dt.timezone.utc)
    now = _dt.datetime.now(_dt.timezone.utc)
    return (now - last).days >= _resuggest_days()


def propose(ctx, agent: str, pattern_key: str, title: str, body: str,
           action_type: str, payload: dict, reasoning: str,
           affected_entity: str = "") -> dict | None:
    """Returns the submit_action result, or None if silenced (drift-
    pruned) or still inside the post-rejection resuggest window."""
    from ..automation.executors import submit_action

    if _drift_silenced(ctx, agent, action_type):
        return None
    dedup_key = f"life_{agent}_{pattern_key}"
    if not _resuggest_ok(ctx, dedup_key):
        return None
    result = submit_action(
        ctx, 2, action_type, title=title, body=body, payload=payload,
        source=f"life_{agent}", dedup_key=dedup_key, reasoning=reasoning,
        risk="write", affected_entity=affected_entity)
    if result.get("status") == "duplicate":
        return None
    try:
        ctx.events().emit(
            "life.pattern_detected",
            {"agent": agent, "pattern_key": pattern_key, "action_type": action_type,
             "summary": f"{agent}: {pattern_key}"},
            source=f"life_{agent}")
    except Exception:
        pass
    return result


def _should_renotify(ctx, key: str, days: int) -> bool:
    row = ctx.collab.conn.execute(
        "SELECT value FROM prefs WHERE key=?", (key,)).fetchone()
    if row is None:
        return True
    try:
        last = _dt.datetime.fromisoformat(row["value"])
    except Exception:
        return True
    return (_dt.datetime.now(_dt.timezone.utc) - last).days >= days


def _mark_notified(ctx, key: str) -> None:
    ctx.collab.conn.execute(
        "INSERT INTO prefs(key,value) VALUES(?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, _dt.datetime.now(_dt.timezone.utc).isoformat()))
    ctx.collab.conn.commit()


def _gentle_nudge(ctx, agent: str, pattern_key: str, title: str, body: str) -> bool:
    """Advisory-only re-suggestion (no write) — used where the spec calls
    for 'ONE gentle re-suggestion per window' rather than a formal
    proposal (e.g. activity's gym-absence nudge to resume an ALREADY-
    existing habit, not propose a new one)."""
    key = f"life_nudge_{agent}_{pattern_key}"
    if not _should_renotify(ctx, key, _resuggest_days()):
        return False
    try:
        from ..events.factory import get_events
        from ..agents.reactive import _emit_insight
        events = get_events(ctx.user_id, ctx.collab, ctx=ctx)
        _emit_insight(events, ctx, f"life_{agent}", title, body)
    except Exception:
        pass
    try:
        ctx.notify_store().create(type=f"life_{agent}_nudge", title=title, body=body,
                                  priority="normal")
    except Exception:
        pass
    _mark_notified(ctx, key)
    return True


# ---------------------------------------------------------------------------
# 1. commute
# ---------------------------------------------------------------------------

def commute_check(ctx) -> list[dict]:
    out = []
    baseline = day_type_baseline(ctx, "office_minutes", "weekday", exclude_days=7)
    week_rows = [r for r in _trailing_rows(ctx, 7, "weekday") if not r.get("grace")]
    week_vals = [r["office_minutes"] for r in week_rows if r.get("office_minutes") is not None]
    if baseline and week_vals:
        week_mean = sum(week_vals) / len(week_vals)
        if week_mean > baseline["mean"] * 1.15:
            typical_left = _median_time([r.get("left_office_at") for r in week_rows]) or "18:30"
            result = propose(
                ctx, "commute", "leave_by",
                title=f"Leave office by {typical_left}?",
                body=(f"This week's office time averaged {week_mean:.0f} min/day vs your "
                     f"{baseline_weeks()}-week weekday baseline of {baseline['mean']:.0f} "
                     f"min/day ({len(week_vals)} days this week, {baseline['n']} baseline days)."),
                action_type="propose_habit",
                payload={"title": f"Leave office by {typical_left}", "frequency": "daily",
                        "link": {"signal_type": "left_office_before",
                                "signal_params": {"kind": "office", "before": typical_left},
                                "mode": "auto_suggest_check"}},
                reasoning=(f"office_minutes {week_mean:.0f} vs baseline {baseline['mean']:.0f} "
                          f"(+{(week_mean / baseline['mean'] - 1) * 100:.0f}%)."))
            if result:
                out.append(result)

    late_rows = [r for r in _trailing_rows(ctx, 14) if not r.get("grace")
                and r.get("home_arrival_at") and r["home_arrival_at"] > "21:00"]
    if len(late_rows) >= 4:
        result = propose(
            ctx, "commute", "late_arrivals",
            title="Repeated after-9pm arrivals — adjust evening targets?",
            body=f"{len(late_rows)} of the last 14 days you arrived home after 9pm.",
            action_type="propose_goal",
            payload={"title": "Earlier evenings", "domain": "life"},
            reasoning=f"{len(late_rows)} post-9pm home arrivals in the last 14 days.")
        if result:
            out.append(result)
    return out


# ---------------------------------------------------------------------------
# 2. meals
# ---------------------------------------------------------------------------

def meals_check(ctx) -> list[dict]:
    out = []
    week_rows = _trailing_rows(ctx, 7)
    late_orders = sum(r.get("late_night_orders") or 0 for r in week_rows)
    if late_orders >= _LATE_NIGHT_WEEKLY_THRESHOLD:
        result = propose(
            ctx, "meals", "cook_habit",
            title="Cook at home more often?",
            body=f"{late_orders} food-delivery orders in the last 7 days.",
            action_type="propose_habit",
            payload={"title": "Cook dinner at home", "frequency": "daily",
                    "link": {"signal_type": "txn_absence",
                            "signal_params": {"merchant_tokens": list(_FOOD_DELIVERY_TOKENS)},
                            "mode": "auto_suggest_check"}},
            reasoning=f"{late_orders} delivery-app orders in 7 days (threshold {_LATE_NIGHT_WEEKLY_THRESHOLD}).")
        if result:
            out.append(result)

    fe = ctx.open_finance()
    try:
        from ..patterns import merchant_cadences
        cadences = merchant_cadences(fe)
    finally:
        fe.close()
    cafe_cad = next((c for c in cadences if _merchant_matches(c["merchant"], _CAFE_TOKENS)), None)
    if cafe_cad:
        monthly_savings = cafe_cad["typical_amount"] * (30 / max(cafe_cad["gap_days"], 1))
        result = propose(
            ctx, "meals", "home_brew",
            title="Home-brew instead of café runs?",
            body=(f"Regular café cadence detected (~every {cafe_cad['gap_days']} days, "
                 f"~{cafe_cad['typical_amount']:.0f} each) — home-brewing could save "
                 f"~{monthly_savings:.0f}/month."),
            action_type="propose_habit",
            payload={"title": "Home-brew coffee", "frequency": "daily",
                    "link": {"signal_type": "txn_absence",
                            "signal_params": {"merchant_tokens": list(_CAFE_TOKENS)},
                            "mode": "auto_suggest_check"}},
            reasoning=f"Café cadence every {cafe_cad['gap_days']} days, ~{monthly_savings:.0f}/month.")
        if result:
            out.append(result)
    return out


# ---------------------------------------------------------------------------
# 3. sleep
# ---------------------------------------------------------------------------

def _sleep_floor_minutes(ctx) -> float:
    profile = ctx.store.get_health_profile(ctx.user_id)
    if profile:
        target = (profile.get("targets") or {}).get("sleep_target")
        if target and isinstance(target.get("value"), dict):
            min_hours = target["value"].get("min_hours")
            if min_hours:
                return float(min_hours) * 60
    return 360.0   # 6h honest generic floor with no accepted target on file


def sleep_check(ctx) -> list[dict]:
    out = []
    rows = sorted((r for r in _trailing_rows(ctx, 10) if not r.get("grace")),
                 key=lambda r: r["date"], reverse=True)
    floor = _sleep_floor_minutes(ctx)
    short_streak = 0
    for r in rows:
        if r.get("sleep_estimate_min") is not None and r["sleep_estimate_min"] < floor:
            short_streak += 1
        else:
            break
    if short_streak >= _SHORT_SLEEP_STREAK_DAYS:
        result = propose(
            ctx, "sleep", "wind_down",
            title="Short sleep streak — try a wind-down routine?",
            body=f"{short_streak} consecutive days under your {floor:.0f}min sleep floor.",
            action_type="propose_habit",
            payload={"title": "Wind-down routine", "frequency": "daily",
                    "link": {"signal_type": "sleep_window_met", "signal_params": {},
                            "mode": "auto_suggest_check"}},
            reasoning=f"{short_streak}-day short-sleep streak (floor {floor:.0f}min).")
        if result:
            out.append(result)
        goal_result = propose(
            ctx, "sleep", "improve_goal",
            title="Improve sleep window",
            body=f"{short_streak} consecutive short-sleep days — worth a tracked goal.",
            action_type="propose_goal",
            payload={"title": "Improve sleep window", "domain": "life"},
            reasoning=f"{short_streak}-day short-sleep streak.")
        if goal_result:
            out.append(goal_result)

    post_midnight = [r for r in rows if r.get("sleep_window_start")
                     and "00:00" <= r["sleep_window_start"] <= "05:00"]
    if len(post_midnight) >= _POST_MIDNIGHT_STREAK_DAYS:
        result = propose(
            ctx, "sleep", "device_down",
            title="Device-down by 11pm?",
            body=f"{len(post_midnight)} of the last 10 days your sleep window started after midnight.",
            action_type="propose_habit",
            payload={"title": "Device-down by 11pm", "frequency": "daily"},   # unlinked — no device signal exists
            reasoning=f"{len(post_midnight)} post-midnight sleep-window starts in 10 days.")
        if result:
            out.append(result)
    return out


# ---------------------------------------------------------------------------
# 4. activity
# ---------------------------------------------------------------------------

def activity_check(ctx) -> list[dict]:
    from ..geo import GeoStore
    from ..patterns import cadence

    out = []
    gs = GeoStore(ctx.collab)
    gym_place = next((p for p in gs.list_places() if p.get("kind") == "gym"), None)
    if not gym_place:
        return out
    rows = gs.db.conn.execute(
        "SELECT entered_at FROM geo_visits WHERE place_id=? ORDER BY entered_at",
        (gym_place["id"],)).fetchall()
    dates = [r["entered_at"][:10] for r in rows]
    cad = cadence(dates)
    existing_links = [l for l in ctx.store.list_habit_links(ctx.user_id)
                      if l["signal_type"] == "geo_place_visit" and l["signal_params"].get("kind") == "gym"]

    if cad and not existing_links:
        result = propose(
            ctx, "activity", "gym_habit",
            title="Track your gym visits automatically?",
            body=f"Regular gym cadence detected (~every {cad['gap_days']} days, {cad['occurrences']} visits).",
            action_type="propose_habit",
            payload={"title": "Workout", "frequency": "daily",
                    "link": {"signal_type": "geo_place_visit", "signal_params": {"kind": "gym"},
                            "mode": "auto_complete"}},
            reasoning=f"Gym visit cadence every {cad['gap_days']} days, {cad['occurrences']} occurrences.")
        if result:
            out.append(result)
    elif existing_links and cad and dates:
        last_visit = _dt.date.fromisoformat(dates[-1])
        days_since = (_dt.date.today() - last_visit).days
        if days_since >= _GYM_ABSENCE_DAYS:
            _gentle_nudge(ctx, "activity", "gym_absence",
                         "It's been a while since the gym",
                         f"{days_since} days since your last gym visit — you used to go "
                         f"about every {cad['gap_days']} days.")
    return out


# ---------------------------------------------------------------------------
# 5. reading
# ---------------------------------------------------------------------------

def reading_check(ctx) -> list[dict]:
    from ..patterns import cadence

    rows = ctx.collab.conn.execute(
        "SELECT ts FROM activities WHERE kind='learning' ORDER BY ts").fetchall()
    dates = [r["ts"][:10] for r in rows]
    cad = cadence(dates)
    existing = [l for l in ctx.store.list_habit_links(ctx.user_id) if l["signal_type"] == "reading_minutes"]
    if not (cad and not existing):
        return []
    result = propose(
        ctx, "reading", "read_habit",
        title="Auto-track your reading habit?",
        body=f"Regular learning engagement detected (~every {cad['gap_days']} days, {cad['occurrences']} sessions).",
        action_type="propose_habit",
        payload={"title": "Read / learn", "frequency": "daily",
                "link": {"signal_type": "reading_minutes", "signal_params": {"min_minutes": 10},
                        "mode": "auto_complete"}},
        reasoning=f"Learning-activity cadence every {cad['gap_days']} days, {cad['occurrences']} sessions.")
    return [result] if result else []


# ---------------------------------------------------------------------------
# 6. meeting-load
# ---------------------------------------------------------------------------

def meeting_load_check(ctx) -> list[dict]:
    out = []
    # meeting_count/focus_blocks are honestly NULL until a real calendar
    # signal source lands (L2's aggregator stubs them None today) — this
    # half of the agent is a documented no-op until then, never fabricated.
    heavy_weeks = [r for r in _trailing_rows(ctx, 14)
                  if (r.get("meeting_count") or 0) >= 6 and (r.get("focus_blocks") or 0) == 0]
    if len(heavy_weeks) >= _HEAVY_MEETING_WEEKS:
        result = propose(
            ctx, "meeting_load", "calendar_block",
            title="Block focus time on your calendar?",
            body=f"{len(heavy_weeks)} recent days with 6+ meetings and zero focus blocks.",
            action_type="propose_habit",
            payload={"title": "Protect a focus block", "frequency": "daily"},
            reasoning=f"{len(heavy_weeks)} heavy-meeting/zero-focus days recently.")
        if result:
            out.append(result)

    weekend_office = [r for r in _trailing_rows(ctx, 28, "weekend")
                      if (r.get("office_minutes") or 0) > 0 and not r.get("grace")]
    if len(weekend_office) >= _WEEKEND_OFFICE_STREAK:
        result = propose(
            ctx, "meeting_load", "protect_weekend",
            title="Protect a weekend?",
            body=f"{len(weekend_office)} of the last 4 weekends included office time.",
            action_type="propose_goal",
            payload={"title": "Protect a weekend", "domain": "life"},
            reasoning=f"{len(weekend_office)} office weekends in the trailing 4.")
        if result:
            out.append(result)
    return out


# ---------------------------------------------------------------------------
# 7. admin
# ---------------------------------------------------------------------------

def admin_check(ctx) -> list[dict]:
    out = []
    fe = ctx.open_finance()
    try:
        subs = fe.list_subscriptions(status=None)
    finally:
        fe.close()
    today = _dt.date.today()
    for s in subs:
        if "insurance" not in (s.get("name") or "").lower():
            continue
        rd = s.get("renewal_date")
        if not rd:
            continue
        try:
            d = _dt.date.fromisoformat(str(rd)[:10])
        except ValueError:
            continue
        days_away = (d - today).days
        if 0 <= days_away <= _DEADLINE_HORIZON_DAYS:
            result = propose(
                ctx, "admin", f"insurance_{s['id']}",
                title=f"Insurance renewal: {s['name']}",
                body=f"{s['name']} renews {rd} ({days_away} day(s) away).",
                action_type="propose_goal",
                payload={"title": f"Renew {s['name']}", "domain": "life", "target_date": str(rd)[:10]},
                reasoning=f"subscriptions.renewal_date within {_DEADLINE_HORIZON_DAYS} days.")
            if result:
                out.append(result)

    from ..jurisdictions import PackError, load_pack, upcoming_deadlines
    jurisdictions = ctx._extras.get("jurisdictions") or ["india"]
    for jid in jurisdictions:
        try:
            dls = upcoming_deadlines(load_pack(jid), horizon_days=_DEADLINE_HORIZON_DAYS)
        except PackError:
            continue
        for d in dls:
            if d.get("kind") != "compliance":
                continue
            result = propose(
                ctx, "admin", f"deadline_{jid}_{d['name']}",
                title=f"{d['name']} deadline approaching",
                body=f"{d['name']} in {jid} due {d['date']} ({d['days_away']} day(s)).",
                action_type="propose_goal",
                payload={"title": f"Prepare for {d['name']}", "domain": "life",
                        "target_date": d["date"]},
                reasoning=f"jurisdiction pack compliance deadline within {_DEADLINE_HORIZON_DAYS} days.")
            if result:
                out.append(result)
    return out


# ---------------------------------------------------------------------------
# 8. seasonal — pack data, not code (seasonal_notes JSON already in every pack)
# ---------------------------------------------------------------------------

def seasonal_check(ctx, as_of: str | None = None) -> list[dict]:
    """as_of is injectable for tests (the real check is 'is the lookahead
    window about to cross into a listed season', which is otherwise
    untestable without controlling today's date)."""
    from ..jurisdictions import PackError, load_pack

    out = []
    jurisdictions = ctx._extras.get("jurisdictions") or ["india"]
    today = _dt.date.fromisoformat(as_of) if as_of else _dt.date.today()
    lookahead = today + _dt.timedelta(days=_SEASONAL_LOOKAHEAD_DAYS)
    for jid in jurisdictions:
        try:
            pack = load_pack(jid)
        except PackError:
            continue
        for note in pack.get("seasonal_notes") or []:
            months = note.get("months") or []
            if lookahead.month in months and today.month not in months:
                key = f"{jid}_{'-'.join(map(str, months))}_{today.year}"
                result = propose(
                    ctx, "seasonal", key,
                    title=f"Heads up: {note['note']}",
                    body=f"{note['note']} (starts within {_SEASONAL_LOOKAHEAD_DAYS} days, {jid} pack).",
                    action_type="propose_goal",
                    payload={"title": f"Seasonal: {note['note'][:60]}", "domain": "life"},
                    reasoning=f"jurisdiction pack seasonal_notes covers month {lookahead.month}.")
                if result:
                    out.append(result)
    return out


# ---------------------------------------------------------------------------
# 9. social — extends patterns.person_cadences
# ---------------------------------------------------------------------------

def social_check(ctx) -> list[dict]:
    from ..patterns import person_cadences

    fe = ctx.open_finance()
    try:
        cadences = person_cadences(fe)
    finally:
        fe.close()
    out = []
    today = _dt.date.today()
    for c in cadences:
        next_due = _dt.date.fromisoformat(c["next_due"])
        days_over = (today - next_due).days - c.get("tolerance_days", 0)
        if days_over <= 0:
            continue
        person = c["person"]
        result = propose(
            ctx, "social", f"call_{person[:30]}",
            title=f"Reconnect with {person}?",
            body=(f"Usual cadence every {c['gap_days']} days; last contact {c['last_date']}, "
                 f"{days_over} day(s) overdue."),
            action_type="propose_habit",
            payload={"title": f"Call {person}", "frequency": "daily"},   # unlinked — no call-detection signal exists
            reasoning=f"Person cadence broken by {days_over} days beyond tolerance.")
        if result:
            out.append(result)
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

_CHECKS = {
    "commute": commute_check, "meals": meals_check, "sleep": sleep_check,
    "activity": activity_check, "reading": reading_check,
    "meeting_load": meeting_load_check, "admin": admin_check,
    "seasonal": seasonal_check, "social": social_check,
}


def run_all(ctx) -> dict:
    from .. import config
    out = {}
    for name, fn in _CHECKS.items():
        if not config.agent_enabled(f"life_{name}"):
            out[name] = {"skipped": "disabled"}
            continue
        try:
            results = fn(ctx)
            out[name] = {"proposed": len(results)}
        except Exception as exc:
            out[name] = {"error": str(exc)[:200]}
    return out
