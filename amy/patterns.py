"""Life-pattern detection (CONTEXT_PLAN C4) — behavior cadences from data.

subscription_detect generalized: instead of recurring *charges*, detect
recurring *behavior* — grocery run every ~7 days, fuel every ~12 — and when
the next cycle comes due, propose the task before the user thinks of it.
The proposal carries a place_tag, so the moment it's approved the C1 errand
agent will remind about it at the right place.

The cadence math is generic (dates in → rhythm out) so C5 can point it at
people (transfer cadences) unchanged. Pure statistics, no LLM.
"""
from __future__ import annotations

import datetime as _dt
import re
import statistics

MIN_OCCURRENCES = 4        # dates needed before a rhythm is believable
MAX_GAP_DAYS = 45          # slower than ~monthly isn't a "run", it's an event
LOOKBACK_DAYS = 120
# a rhythm is regular when gaps cluster: median |gap − median| ≤ max(2, 30%)
_TOLERANCE_FLOOR_DAYS = 2
_TOLERANCE_RATIO = 0.3


def cadence(dates: list[str]) -> dict | None:
    """Distinct ISO dates → rhythm, or None if too few/too irregular.

    Returns {gap_days, next_due, last_date, occurrences, tolerance_days}.
    """
    days = sorted({d[:10] for d in dates if d})
    if len(days) < MIN_OCCURRENCES:
        return None
    ds = [_dt.date.fromisoformat(d) for d in days]
    gaps = [(b - a).days for a, b in zip(ds, ds[1:])]
    med = statistics.median(gaps)
    if not (1 <= med <= MAX_GAP_DAYS):
        return None
    tolerance = max(_TOLERANCE_FLOOR_DAYS, med * _TOLERANCE_RATIO)
    if statistics.median(abs(g - med) for g in gaps) > tolerance:
        return None
    gap = int(round(med))
    return {"gap_days": gap,
            "last_date": days[-1],
            "next_due": (ds[-1] + _dt.timedelta(days=gap)).isoformat(),
            "occurrences": len(days),
            "tolerance_days": int(round(tolerance))}


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in re.split(r"[^A-Za-z]+", text or "")
            if len(t) >= 4}


def merchant_cadences(fe) -> list[dict]:
    """Recurring-behavior merchants over the lookback window (debits only)."""
    since = (_dt.date.today() - _dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    by_merchant: dict[str, list[str]] = {}
    amounts: dict[str, list[float]] = {}
    for t in fe.list_transactions(limit=2000, since=since):
        m = (t.get("merchant") or "").strip()
        if not m or (t.get("amount") or 0) >= 0:
            continue
        by_merchant.setdefault(m, []).append(t["date"])
        amounts.setdefault(m, []).append(abs(t["amount"]))
    out = []
    for merchant, dates in by_merchant.items():
        c = cadence(dates)
        if c:
            c["merchant"] = merchant
            c["typical_amount"] = round(statistics.median(amounts[merchant]))
            out.append(c)
    return out


def _place_tag_for(merchant: str, places: list[dict]) -> str:
    """Prefill the errand-agent match key: a saved place whose name/kind
    shares a token with the merchant wins; else the merchant's own token."""
    mtoks = _tokens(merchant)
    for p in places:
        if mtoks & _tokens(f"{p.get('name', '')} {p.get('kind', '')}"):
            return (p.get("kind") or p.get("name") or "").lower()
    return max(mtoks, key=len) if mtoks else ""


# ---------------------------------------------------------------------------
# C5 — the same cadence math pointed at people instead of merchants
# ---------------------------------------------------------------------------

# categories whose counterparty is a person, not a shop
_PERSON_CATEGORIES = {"transfer", "family", "custodial disbursement", "gift",
                      "gifts"}
_NUDGE_WINDOW_DAYS = 3     # nudge while overdue-by-tolerance, then go quiet


def person_cadences(fe) -> list[dict]:
    """Recurring transfer rhythms to people (debits in person categories)."""
    since = (_dt.date.today() - _dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    by_person: dict[str, list[str]] = {}
    amounts: dict[str, list[float]] = {}
    for t in fe.list_transactions(limit=2000, since=since):
        cat = (t.get("category") or "").strip().lower()
        person = (t.get("merchant") or "").strip()
        if not person or cat not in _PERSON_CATEGORIES:
            continue
        if (t.get("amount") or 0) >= 0:
            continue
        by_person.setdefault(person, []).append(t["date"])
        amounts.setdefault(person, []).append(abs(t["amount"]))
    out = []
    for person, dates in by_person.items():
        c = cadence(dates)
        if c:
            c["person"] = person
            c["typical_amount"] = round(statistics.median(amounts[person]))
            out.append(c)
    return out


def relationship_nudges(ctx) -> dict:
    """Job handler: a regular transfer rhythm has gone quiet → gentle nudge.

    Advisory only (notification + insight, nothing proposed or written), and
    it only speaks in a short window after the rhythm breaks — it must never
    become a daily nag."""
    today = _dt.date.today()
    fe = ctx.open_finance()
    try:
        cadences = person_cadences(fe)
    finally:
        fe.close()
    ns = ctx.notify_store()
    events = ctx.events()
    nudged = 0
    for c in cadences:
        next_due = _dt.date.fromisoformat(c["next_due"])
        days_over = (today - next_due).days - c["tolerance_days"]
        if not (0 < days_over <= _NUDGE_WINDOW_DAYS):
            continue
        gap_actual = (today - _dt.date.fromisoformat(c["last_date"])).days
        reasoning = (
            f"You've sent ~₹{c['typical_amount']:,} to '{c['person']}' every "
            f"~{c['gap_days']} days ({c['occurrences']}x recently), but it's "
            f"now been {gap_actual} days since the last one "
            f"({c['last_date']}). Maybe intentional — flagging once in case "
            "it isn't.")
        ref = f"rel_{c['person'][:40]}_{c['next_due']}"
        if ns.exists_today("relationship_nudge", ref):
            continue
        ns.create(type="relationship_nudge",
                  title=f"It's been a while: {c['person']}",
                  body=reasoning, priority="normal",
                  related_entity={"entity_type": "person", "id": ref,
                                  "person": c["person"]})
        try:
            events.emit("agent.insight",
                        {"agent": "relationship", "summary":
                         f"Transfer rhythm to {c['person']} broke",
                         "reasoning": reasoning, "person": c["person"]},
                        source="relationship_agent")
        except Exception:
            pass
        nudged += 1
    return {"cadences": len(cadences), "nudged": nudged}


def pattern_tasks(ctx) -> dict:
    """Job handler: cadences due now → propose a prefilled task.

    Goes through submit_action at the standard write tier (tier 2 approval by
    default; AMY_AGENT_WRITE_TIER=1 auto-creates + notifies), deduped per
    merchant+cycle so each cycle is proposed exactly once."""
    from .automation.executors import submit_action, _tier_for
    from .geo import GeoStore

    today = _dt.date.today()
    fe = ctx.open_finance()
    try:
        cadences = merchant_cadences(fe)
    finally:
        fe.close()
    places = GeoStore(ctx.collab).list_places()

    open_titles = " ".join(
        r["title"] or "" for r in ctx.collab.conn.execute(
            "SELECT title FROM tasks WHERE done=0").fetchall())
    proposed = duplicates = 0
    for c in cadences:
        next_due = _dt.date.fromisoformat(c["next_due"])
        # due tomorrow at the earliest rung; stale after a whole missed cycle
        if not (next_due - _dt.timedelta(days=1) <= today
                <= next_due + _dt.timedelta(days=c["gap_days"])):
            continue
        # an open task that already matches means there's nothing to propose
        if _tokens(c["merchant"]) & _tokens(open_titles):
            continue
        tag = _place_tag_for(c["merchant"], places)
        title = f"Usual {c['merchant']} run"
        reasoning = (
            f"You've gone to '{c['merchant']}' every ~{c['gap_days']} days "
            f"({c['occurrences']}x in {LOOKBACK_DAYS}d, typically "
            f"₹{c['typical_amount']:,}), last on {c['last_date']} — the next "
            f"cycle is due {c['next_due']}. Approving creates the task; "
            "you'll get a reminder when you're near the place.")
        out = submit_action(
            ctx, tier=_tier_for("write"), action_type="add_task",
            title=f"Task suggestion: {title}",
            body=reasoning, reasoning=reasoning, risk="write",
            payload={"title": title, "place_tag": tag},
            source="pattern_tasks",
            affected_entity=f"merchant={c['merchant']}",
            dedup_key=f"task_{c['merchant'][:40]}_{c['next_due']}")
        if out.get("status") == "duplicate":
            duplicates += 1
        else:
            proposed += 1
    return {"cadences": len(cadences), "proposed": proposed,
            "duplicates": duplicates}
