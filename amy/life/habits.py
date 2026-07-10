"""LIFE AUTOPILOT L4 — habit auto-completion, grace streaks, adaptation.

habit_links (collab.db) map a habit (habits.db row, bridged by id — see
JobCtx.open_habits()) to a signal. mode='auto_complete' checks in at tier 0
(silent); mode='auto_suggest_check' checks in at tier 1 (executes +
notifies, one-tap undo via the existing POST /api/habits/{id}/checkin
done=false — no new undo mechanism needed). Both go through submit_action
directly, never through the tools registry/AGENT_GATE — the auto-
completion signal IS the authorization, same as every other reactive-
agent write in this codebase (pattern_tasks, place_learning, etc.).

Real-time signals (geo_place_visit, left_office_before) evaluate on
context.place_entered/context.place_left. Absence checks (txn_absence)
are DAY-CLOSE ONLY (a day isn't over until it's over — see hard rule in
docs/LIFE_AUTOPILOT.md). Day-close batch evaluates every signal type and
is driven by the life_metrics_daily job right after compute_day, so it
always has that day's life_metrics row as input.

Streak grace: AMY_LIFE_STREAK_GRACE_PER_WEEK (1) missed (non-grace) day
per ISO week is tolerated without breaking the streak; life_metrics.grace
days (away/silent) pause the streak entirely (never counted, never
break it) — hard rule 8. A per-habit grace-per-week override (adaptation)
is stored in prefs as habit_grace_{habit_id}, not a new table.

Adaptation: >=AMY_LIFE_ADAPT_FAIL_WEEKS (3) consecutive failing weeks ->
one tier-2 easing proposal (raises the grace-per-week override) with
miss-pattern evidence; >=6 consecutive effortless weeks -> at most ONE
level-up proposal ever (fixed dedup key, no week suffix). Two rejected
adjust_habit_target proposals for the same habit permanently silence
further adaptation checks for it (counted from the approvals table itself
— no separate table needed, same "existence in approvals is the record"
idiom Part 5's follow-up-check dedup uses). Never auto-archives a habit.

Only frequency='daily' habits are eligible for adaptation — 'daily' is
the only frequency value with real enforced semantics anywhere in this
codebase (HabitEngine.frequency is otherwise a free-text display label);
adaptation on other frequencies is a documented no-op, not built.
"""
from __future__ import annotations

import datetime as _dt
import json

_FOOD_DELIVERY_HABIT_TOKENS = (
    "swiggy", "zomato", "eatsure", "freshmenu", "box8", "faasos",
    "dominos", "mcdonalds", "mcd", "kfc", "pizza", "burger",
)


def _today() -> str:
    return _dt.date.today().isoformat()


def _is_grace_day(ctx, date: str) -> bool:
    m = ctx.store.get_life_metrics(ctx.user_id, date)
    return bool(m and m.get("grace"))


def _habit_done(habits, habit_id: str, date: str) -> bool:
    row = habits.db.execute(
        "SELECT done FROM habit_logs WHERE habit_id=? AND date=?",
        (habit_id, date)).fetchone()
    return bool(row and row["done"])


def _get_habit(habits, habit_id: str) -> dict | None:
    row = habits.db.execute(
        "SELECT * FROM habits WHERE id=?", (habit_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _streak_grace_default() -> int:
    from .. import config
    try:
        return int(config._env("AMY_LIFE_STREAK_GRACE_PER_WEEK", "1"))
    except ValueError:
        return 1


def _adapt_fail_weeks() -> int:
    from .. import config
    try:
        return int(config._env("AMY_LIFE_ADAPT_FAIL_WEEKS", "3"))
    except ValueError:
        return 3


_ADAPT_LEVELUP_WEEKS = 6


def effective_grace_per_week(ctx, habit_id: str) -> int:
    row = ctx.collab.conn.execute(
        "SELECT value FROM prefs WHERE key=?", (f"habit_grace_{habit_id}",)).fetchone()
    if row and row["value"]:
        try:
            return int(row["value"])
        except ValueError:
            pass
    return _streak_grace_default()


def set_grace_per_week(ctx, habit_id: str, value: int) -> None:
    ctx.collab.conn.execute(
        "INSERT INTO prefs(key,value) VALUES(?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"habit_grace_{habit_id}", str(max(0, value))))
    ctx.collab.conn.commit()


# ---------------------------------------------------------------------------
# Grace-aware streak
# ---------------------------------------------------------------------------

def streak_with_grace(ctx, habit_id: str, habits, as_of: str | None = None) -> int:
    """Walks backward from as_of (default today). life_metrics.grace days
    are skipped entirely (pause the streak). Within each ISO week, up to
    effective_grace_per_week non-grace misses are tolerated without
    breaking the streak; the first miss beyond that budget ends it."""
    grace_budget = effective_grace_per_week(ctx, habit_id)
    cur = _dt.date.fromisoformat(as_of) if as_of else _dt.date.today()
    streak = 0
    current_week = cur.isocalendar()[:2]
    misses_this_week = 0
    for _ in range(370):
        wk = cur.isocalendar()[:2]
        if wk != current_week:
            current_week = wk
            misses_this_week = 0
        day_s = cur.isoformat()
        if _is_grace_day(ctx, day_s):
            cur -= _dt.timedelta(days=1)
            continue
        if _habit_done(habits, habit_id, day_s):
            streak += 1
            cur -= _dt.timedelta(days=1)
            continue
        if misses_this_week < grace_budget:
            misses_this_week += 1
            cur -= _dt.timedelta(days=1)
            continue
        break
    return streak


# ---------------------------------------------------------------------------
# Weekly stats + adaptation
# ---------------------------------------------------------------------------

def _week_start(d: _dt.date) -> _dt.date:
    return d - _dt.timedelta(days=d.weekday())   # Monday


def week_stats(ctx, habit_id: str, habits, week_start: _dt.date) -> dict:
    non_grace_days = done_days = 0
    today = _dt.date.today()
    for i in range(7):
        d = week_start + _dt.timedelta(days=i)
        if d > today:
            break
        day_s = d.isoformat()
        if _is_grace_day(ctx, day_s):
            continue
        non_grace_days += 1
        if _habit_done(habits, habit_id, day_s):
            done_days += 1
    grace_budget = effective_grace_per_week(ctx, habit_id)
    missed = non_grace_days - done_days
    judged = non_grace_days >= 4   # majority-grace weeks carry too little signal to judge
    return {
        "week_start": week_start.isoformat(), "non_grace_days": non_grace_days,
        "done_days": done_days, "missed": missed, "judged": judged,
        "failing": judged and missed > grace_budget,
        "effortless": judged and non_grace_days >= 5 and missed == 0,
    }


def _trailing_weeks(n: int, as_of: _dt.date | None = None) -> list[_dt.date]:
    end = _week_start(as_of or _dt.date.today())
    return [end - _dt.timedelta(weeks=i) for i in range(n - 1, -1, -1)]


def _rejection_count(ctx, habit_id: str) -> int:
    rows = ctx.collab.conn.execute(
        "SELECT payload FROM approvals WHERE action_type='adjust_habit_target'"
        " AND status='rejected'").fetchall()
    n = 0
    for r in rows:
        try:
            if json.loads(r["payload"] or "{}").get("habit_id") == habit_id:
                n += 1
        except Exception:
            continue
    return n


def adaptation_silenced(ctx, habit_id: str) -> bool:
    return _rejection_count(ctx, habit_id) >= 2


def check_adaptation(ctx, habit_id: str, habits) -> dict | None:
    """Evaluates one habit for an easing or level-up proposal. Returns the
    submit_action result dict, or None if no adaptation is due."""
    from ..automation.executors import submit_action

    habit = _get_habit(habits, habit_id)
    if not habit or habit["archived"] or habit["frequency"] != "daily":
        return None
    if adaptation_silenced(ctx, habit_id):
        return None

    fail_n = _adapt_fail_weeks()
    weeks_needed = max(fail_n, _ADAPT_LEVELUP_WEEKS)
    starts = _trailing_weeks(weeks_needed)
    stats = [week_stats(ctx, habit_id, habits, s) for s in starts]
    judged = [s for s in stats if s["judged"]]

    fail_slice = judged[-fail_n:] if len(judged) >= fail_n else []
    if fail_slice and all(s["failing"] for s in fail_slice):
        current = effective_grace_per_week(ctx, habit_id)
        new_grace = current + 1
        evidence = "; ".join(
            f"week of {w['week_start']}: missed {w['missed']}/{w['non_grace_days']} days"
            for w in fail_slice)
        result = submit_action(
            ctx, 2, "adjust_habit_target",
            title=f"Ease up on '{habit['title']}'?",
            body=(f"{len(fail_slice)} failing weeks in a row ({evidence}). "
                 f"Proposing to allow {new_grace} missed day(s)/week instead of {current}."),
            payload={"habit_id": habit_id, "direction": "ease",
                    "old_grace_per_week": current, "new_grace_per_week": new_grace},
            source="habit_adaptation",
            dedup_key=f"habit_adapt_ease_{habit_id}_{fail_slice[-1]['week_start']}",
            reasoning=f"{len(fail_slice)} consecutive failing weeks (>{current} missed/week).",
            risk="write", affected_entity=f"habit={habit_id}")
        if result.get("status") != "duplicate":
            return result

    levelup_slice = judged[-_ADAPT_LEVELUP_WEEKS:] if len(judged) >= _ADAPT_LEVELUP_WEEKS else []
    if levelup_slice and all(s["effortless"] for s in levelup_slice):
        current = effective_grace_per_week(ctx, habit_id)
        new_grace = max(0, current - 1)
        result = submit_action(
            ctx, 2, "adjust_habit_target",
            title=f"'{habit['title']}' looks effortless — level up?",
            body=(f"{_ADAPT_LEVELUP_WEEKS} consecutive weeks with zero missed days. "
                 f"Proposing a stricter grace budget ({new_grace} missed day(s)/week "
                 f"instead of {current}) — a harder version of this habit is one option."),
            payload={"habit_id": habit_id, "direction": "levelup",
                    "old_grace_per_week": current, "new_grace_per_week": new_grace},
            source="habit_adaptation",
            dedup_key=f"habit_adapt_levelup_{habit_id}",   # max ONE ever
            reasoning=f"{_ADAPT_LEVELUP_WEEKS} consecutive effortless weeks.",
            risk="write", affected_entity=f"habit={habit_id}")
        if result.get("status") != "duplicate":
            return result
    return None


def check_all_adaptations(ctx) -> list[dict]:
    habits = ctx.open_habits()
    try:
        out = []
        for h in habits.list_habits():
            result = check_adaptation(ctx, h["id"], habits)
            if result:
                out.append(result)
        return out
    finally:
        habits.close()


# ---------------------------------------------------------------------------
# Auto-completion
# ---------------------------------------------------------------------------

def _complete(ctx, events, link: dict, date: str) -> bool:
    """Checks the habit in at the link's tier if not already done that day.
    Returns True if this call caused a fresh completion (dedup — 'exactly
    once' even if the triggering signal fires more than once that day)."""
    from ..automation.executors import submit_action

    habits = ctx.open_habits()
    try:
        already = _habit_done(habits, link["habit_id"], date)
    finally:
        habits.close()
    if already:
        return False

    tier = 0 if link["mode"] == "auto_complete" else 1
    result = submit_action(
        ctx, tier, "complete_habit_check",
        title=f"Habit auto-tracked via {link['signal_type']}",
        body=f"Checked in automatically ({date}) via {link['signal_type']}.",
        payload={"habit_id": link["habit_id"], "date": date,
                "note": f"auto via {link['signal_type']}"},
        source="habit_signals",
        dedup_key=f"habit_complete_{link['habit_id']}_{date}",
        reasoning=f"habit_links signal {link['signal_type']} matched on {date}.",
        risk="write", affected_entity=f"habit={link['habit_id']}")
    if result.get("status") == "duplicate":
        return False
    try:
        eid = events.emit(
            "life.habit_autocompleted",
            {"habit_id": link["habit_id"], "date": date,
             "signal_type": link["signal_type"], "mode": link["mode"]},
            source="habit_signals")
        from ..agents.reactive import _journal
        _journal(ctx, {"id": eid, "type": "life.habit_autocompleted",
                       "payload": {"habit_id": link["habit_id"], "date": date},
                       "ts": None, "source": "habit_signals"})
    except Exception:
        pass
    return True


def on_place_entered(ctx, events, payload: dict) -> int:
    """Real-time geo_place_visit evaluation."""
    place_id = payload.get("place_id") or ""
    kind = (payload.get("kind") or "").strip().lower()
    links = [l for l in ctx.store.list_habit_links(ctx.user_id)
            if l["signal_type"] == "geo_place_visit"]
    completed = 0
    for link in links:
        params = link["signal_params"]
        if params.get("place_id") == place_id or (params.get("kind") and params["kind"] == kind):
            if _complete(ctx, events, link, _today()):
                completed += 1
    return completed


def on_place_left(ctx, events, payload: dict, now_hhmm: str | None = None) -> int:
    """Real-time left_office_before evaluation — 'left office by 6' checks
    the moment the place is left, not at day-close. now_hhmm is
    injectable for tests; defaults to the real current time."""
    kind = (payload.get("kind") or "").strip().lower()
    now_hhmm = now_hhmm or _dt.datetime.now().strftime("%H:%M")
    links = [l for l in ctx.store.list_habit_links(ctx.user_id)
            if l["signal_type"] == "left_office_before"]
    completed = 0
    for link in links:
        params = link["signal_params"]
        want_kind = (params.get("kind") or "office").strip().lower()
        before = params.get("before")
        if kind != want_kind or not before:
            continue
        if now_hhmm <= before:
            if _complete(ctx, events, link, _today()):
                completed += 1
    return completed


def _place_visited(ctx, date: str, place_id: str | None, kind: str | None) -> bool:
    if place_id:
        row = ctx.collab.conn.execute(
            "SELECT 1 FROM geo_visits WHERE place_id=? AND substr(entered_at,1,10)=?"
            " LIMIT 1", (place_id, date)).fetchone()
        return row is not None
    if kind:
        row = ctx.collab.conn.execute(
            "SELECT 1 FROM geo_visits v JOIN geo_places p ON p.id=v.place_id"
            " WHERE p.kind=? AND substr(v.entered_at,1,10)=? LIMIT 1",
            (kind, date)).fetchone()
        return row is not None
    return False


def evaluate_day_close(ctx, date: str) -> int:
    """Batch evaluation for every habit_links row — the only path for
    txn_absence/txn_presence/reading_minutes/sleep_window_met (day isn't
    over until it's over), and a catch-all for geo_place_visit/
    left_office_before in case the real-time path missed a fix."""
    from ..events.factory import get_events

    events = get_events(ctx.user_id, ctx.collab, ctx=ctx)
    links = ctx.store.list_habit_links(ctx.user_id)
    metrics = ctx.store.get_life_metrics(ctx.user_id, date) or {}
    completed = 0
    for link in links:
        params = link["signal_params"]
        st = link["signal_type"]
        matched = False
        if st == "geo_place_visit":
            matched = _place_visited(ctx, date, params.get("place_id"), params.get("kind"))
        elif st == "left_office_before":
            left_at = metrics.get("left_office_at")
            before = params.get("before")
            matched = bool(left_at and before and left_at <= before)
        elif st == "txn_absence":
            matched = not _txn_matches(ctx, date, params)
        elif st == "txn_presence":
            matched = _txn_matches(ctx, date, params)
        elif st == "reading_minutes":
            rm = metrics.get("reading_minutes")
            matched = rm is not None and rm >= float(params.get("min_minutes", 10))
        elif st == "sleep_window_met":
            matched = metrics.get("sleep_estimate_min") is not None
        elif st == "capture_meal":
            mc = metrics.get("meal_captures")
            matched = mc is not None and mc >= int(params.get("min_captures", 1))
        elif st == "steps":
            from .health_data import fetch_device_activity
            device = fetch_device_activity(ctx, date)
            matched = (device.get("available") and device.get("steps") is not None
                      and device["steps"] >= int(params.get("min_steps", 5000)))
        elif st == "workouts":
            from .health_data import fetch_device_activity
            device = fetch_device_activity(ctx, date)
            matched = (device.get("available") and device.get("workouts") is not None
                      and device["workouts"] >= int(params.get("min_workouts", 1)))
        if matched and _complete(ctx, events, link, date):
            completed += 1
    return completed


def _txn_matches(ctx, date: str, params: dict) -> bool:
    fe = ctx.open_finance()
    try:
        txns = fe.list_transactions(limit=500, since=date, until=date)
    finally:
        fe.close()
    tokens = params.get("merchant_tokens") or list(_FOOD_DELIVERY_HABIT_TOKENS)
    category = params.get("category")
    for t in txns:
        if (t.get("amount") or 0) >= 0:
            continue
        if category and (t.get("category") or "").lower() == category.lower():
            return True
        merchant = (t.get("merchant") or "").lower()
        if any(tok in merchant for tok in tokens):
            return True
    return False


# ---------------------------------------------------------------------------
# Add-flow link suggestions (backend logic; L7 wires the UI affordance)
# ---------------------------------------------------------------------------

_TITLE_SUGGESTIONS = (
    (("gym", "workout", "exercise"), "geo_place_visit", {"kind": "gym"}, "auto_complete"),
    (("read", "reading"), "reading_minutes", {"min_minutes": 10}, "auto_suggest_check"),
    (("sleep", "bed", "wind down", "wind-down"), "sleep_window_met", {}, "auto_suggest_check"),
    (("cook", "home cook", "no takeout", "no delivery"), "txn_absence",
     {"merchant_tokens": list(_FOOD_DELIVERY_HABIT_TOKENS)}, "auto_suggest_check"),
    (("leave office", "leave work", "office by"), "left_office_before",
     {"kind": "office", "before": "18:00"}, "auto_suggest_check"),
)


def suggest_link_for_title(title: str) -> dict | None:
    """Suggestion only, never forced — the Add-habit flow offers this, the
    user opts in. Returns {signal_type, signal_params, mode} or None."""
    t = (title or "").lower()
    for keywords, signal_type, params, mode in _TITLE_SUGGESTIONS:
        if any(k in t for k in keywords):
            return {"signal_type": signal_type, "signal_params": params, "mode": mode}
    return None
