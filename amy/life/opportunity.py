"""LIFE AUTOPILOT L9 — place-opportunity dispatcher.

ONE dispatcher on context.place_entered (dwell only — existing geo
hysteresis already filters pass-bys, see amy/geo/store.py's LEAVE_FACTOR)
iterating amy/life/opportunity_rules.RULES generically. New rule types go
in that module only — this dispatcher must never grow rule-specific code.

Anti-nag controls (all four independent, per the spec):
  - dedup per rule x place x need: NotificationStore.exists_today() keyed
    on a dedup string embedding rule/place_id/need_key.
  - AMY_LIFE_OPP_MAX_PER_DAY: a prefs-table counter per calendar day.
  - grace suppression: the most recently computed life_metrics row's
    grace flag (today's own day_type isn't known until tomorrow's job
    run, so 'yesterday' is the honest proxy for 'currently in a
    low-signal/away stretch').
  - drift pruning per rule category: two dismissals (via the dismiss()
    endpoint below) permanently silence that rule — tracked in prefs,
    NOT amy/automation/drift.py's approval-rejection signals (L9 fires
    notifications, not approvals, so there is nothing for drift.py's
    machinery to see).

gym_prompt is the one exception that's a real write (tier 0, one-tap
check) rather than an advisory notification — routed through
submit_action directly like every other auto-completion in this
codebase, never through the tools registry/AGENT_GATE.
"""
from __future__ import annotations

import datetime as _dt

from .opportunity_rules import RULES


def _max_per_day() -> int:
    from .. import config
    try:
        return int(config._env("AMY_LIFE_OPP_MAX_PER_DAY", "3"))
    except ValueError:
        return 3


def _daily_count_key(date: str) -> str:
    return f"life_opp_count_{date}"


def _daily_count(ctx, date: str) -> int:
    row = ctx.collab.conn.execute(
        "SELECT value FROM prefs WHERE key=?", (_daily_count_key(date),)).fetchone()
    try:
        return int(row["value"]) if row and row["value"] else 0
    except ValueError:
        return 0


def _increment_daily_count(ctx, date: str) -> None:
    n = _daily_count(ctx, date) + 1
    ctx.collab.conn.execute(
        "INSERT INTO prefs(key,value) VALUES(?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_daily_count_key(date), str(n)))
    ctx.collab.conn.commit()


def _grace_suppressed(ctx) -> bool:
    yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    m = ctx.store.get_life_metrics(ctx.user_id, yesterday)
    return bool(m and m.get("grace"))


def _dismiss_key(rule_name: str) -> str:
    return f"life_opp_dismiss_{rule_name}"


def _dismiss_count(ctx, rule_name: str) -> int:
    row = ctx.collab.conn.execute(
        "SELECT value FROM prefs WHERE key=?", (_dismiss_key(rule_name),)).fetchone()
    try:
        return int(row["value"]) if row and row["value"] else 0
    except ValueError:
        return 0


def _category_silenced(ctx, rule_name: str) -> bool:
    return _dismiss_count(ctx, rule_name) >= 2


def _fire(ctx, events, rule_name: str, dedup_key: str, trigger: dict) -> dict | None:
    tier0 = trigger.get("tier0_action")
    if tier0:
        from ..automation.executors import submit_action
        result = submit_action(
            ctx, 0, tier0["action_type"], title=trigger["title"], body=trigger["body"],
            payload=tier0["payload"], source=f"life_opp_{rule_name}", dedup_key=dedup_key,
            reasoning=trigger["body"], risk="write")
        return result if result.get("status") != "duplicate" else None

    ns = ctx.notify_store()
    if ns.exists_today(f"life_opp_{rule_name}", dedup_key):
        return None
    nid = ns.create(
        type=f"life_opp_{rule_name}", title=trigger["title"], body=trigger["body"],
        priority="normal",
        related_entity={"entity_type": "life_opportunity", "rule": rule_name,
                        "dedup_key": dedup_key, "id": dedup_key})
    try:
        from ..agents.reactive import _emit_insight
        from ..events.factory import get_events
        ev = get_events(ctx.user_id, ctx.collab, ctx=ctx) if events is None else events
        _emit_insight(ev, ctx, f"life_opp_{rule_name}", trigger["title"], trigger["body"])
    except Exception:
        pass
    return {"notification_id": nid}


def dispatch(ctx, events, payload: dict) -> int:
    place_id = payload.get("place_id") or ""
    kind = (payload.get("kind") or "").strip().lower()
    name = (payload.get("name") or "").strip()
    if not place_id or not kind:
        return 0   # no-kind places skip — the tag-your-places flow is the fix, never guessing
    if _grace_suppressed(ctx):
        return 0

    today = _dt.date.today().isoformat()
    fired = 0
    place = {"place_id": place_id, "name": name, "kind": kind}
    for rule_name, fn in RULES.items():
        if _daily_count(ctx, today) >= _max_per_day():
            break
        if _category_silenced(ctx, rule_name):
            continue
        try:
            trigger = fn(ctx, place)
        except Exception:
            continue
        if not trigger:
            continue
        dedup_key = f"life_opp_{rule_name}_{place_id}_{trigger['need_key']}"
        result = _fire(ctx, events, rule_name, dedup_key, trigger)
        if result:
            fired += 1
            _increment_daily_count(ctx, today)
    return fired


def dismiss(ctx, notification_id: str) -> dict:
    """Records the drift signal a dismiss represents — two dismissals of
    the same rule category permanently silence it (checked by
    _category_silenced above, consulted on every future dispatch)."""
    ns = ctx.notify_store()
    notes = ns.list(limit=500)
    note = next((n for n in notes if n["id"] == notification_id), None)
    if not note:
        return {"ok": False, "error": "not found"}
    ns.mark_read(notification_id)
    rule = (note.get("related_entity") or {}).get("rule")
    if not rule:
        return {"ok": True, "rule": None}
    key = _dismiss_key(rule)
    n = _dismiss_count(ctx, rule) + 1
    ctx.collab.conn.execute(
        "INSERT INTO prefs(key,value) VALUES(?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(n)))
    ctx.collab.conn.commit()
    return {"ok": True, "rule": rule, "dismiss_count": n, "silenced": n >= 2}
