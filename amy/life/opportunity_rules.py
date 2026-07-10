"""LIFE AUTOPILOT L9 — place-opportunity rules table.

Each rule is a plain function `(ctx, place) -> dict | None` registered
under @rule("name"). place = {"place_id","name","kind"} — coordinates
never reach this module (the dispatcher only ever passes the
context.place_entered payload's identity fields). A rule returns None
when there is no REAL pending need (hard requirement: a trigger with no
pending need never fires) — never a guess, never LLM-phrased (pure local
rules, hard rule 6).

New rule types are added here ONLY — amy/life/opportunity.py's dispatcher
iterates this registry generically and must never be touched to add one.

Return shape when a need exists:
    {"need_key": str,          # part of the dedup key — WHAT the need is
     "title": str, "body": str,   # body must include the evidence
     "tier0_action": {"action_type", "payload"} | None}   # gym_prompt only
"""
from __future__ import annotations

import datetime as _dt
import re as _re

RULES: dict[str, callable] = {}


def rule(name: str):
    def deco(fn):
        RULES[name] = fn
        return fn
    return deco


_WORD_RE = _re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> set[str]:
    return set(_WORD_RE.findall((s or "").lower()))


def _fuzzy_hit(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return bool(ta & tb) or any(t in bt or bt in t for t in ta for bt in tb if len(t) > 3 and len(bt) > 3)


_GROCERY_TOKENS = ("bigbasket", "blinkit", "zepto", "grofers", "dmart",
                   "reliancefresh", "reliance fresh", "big bazaar", "more supermarket", "grocery", "grocer")
_CAFE_TOKENS = ("cafe", "coffee", "starbucks", "chaayos", "chai point", "costa", "ccd")


def _merchant_matches(merchant: str, tokens: tuple[str, ...]) -> bool:
    m = (merchant or "").lower()
    return any(t in m for t in tokens)


# ---------------------------------------------------------------------------
# 1. grocery
# ---------------------------------------------------------------------------

@rule("grocery")
def _rule_grocery(ctx, place: dict) -> dict | None:
    if place["kind"] != "grocery":
        return None
    habits = ctx.open_habits()
    try:
        cook_habit = any("cook" in (h["title"] or "").lower() for h in habits.list_habits())
    finally:
        habits.close()
    if not cook_habit:
        return None
    fe = ctx.open_finance()
    try:
        since = (_dt.date.today() - _dt.timedelta(days=10)).isoformat()
        txns = fe.list_transactions(limit=200, since=since)
    finally:
        fe.close()
    recent_grocery = any(_merchant_matches(t.get("merchant", ""), _GROCERY_TOKENS)
                         for t in txns if (t.get("amount") or 0) < 0)
    if recent_grocery:
        return None
    return {"need_key": "no_recent_grocery", "tier0_action": None,
           "title": f"Grocery run at {place['name']}?",
           "body": (f"You have a cook-at-home habit but no grocery purchase in the last "
                   f"10 days — {place['name']} is a grocery-kind place.")}


# ---------------------------------------------------------------------------
# 2. pharmacy
# ---------------------------------------------------------------------------

@rule("pharmacy")
def _rule_pharmacy(ctx, place: dict) -> dict | None:
    if place["kind"] != "pharmacy":
        return None
    fe = ctx.open_finance()
    try:
        from ..commitments import CommitmentEngine
        ce = CommitmentEngine(fe)
        # Refill commitments are an L8 addition (kind='custom', titled
        # 'refill ...'); honestly no-ops until L8 creates real ones.
        open_rows = ce.list("open")
    finally:
        fe.close()
    refill = next((c for c in open_rows if "refill" in (c.get("title") or "").lower()), None)
    if not refill:
        return None
    return {"need_key": f"refill_{refill['id']}", "tier0_action": None,
           "title": f"Refill due: {refill['title']}",
           "body": f"{refill['title']} is an open refill commitment, due {refill['due_date']}."}


# ---------------------------------------------------------------------------
# 3. return_window
# ---------------------------------------------------------------------------

@rule("return_window")
def _rule_return_window(ctx, place: dict) -> dict | None:
    fe = ctx.open_finance()
    try:
        from ..commitments import CommitmentEngine
        ce = CommitmentEngine(fe)
        open_rows = ce.list("open")
    finally:
        fe.close()
    match = next((c for c in open_rows if c["kind"] in ("return_window", "warranty")
                 and _fuzzy_hit(c.get("merchant") or c.get("title") or "", place["name"])), None)
    if not match:
        return None
    return {"need_key": f"return_{match['id']}", "tier0_action": None,
           "title": f"Open {match['kind'].replace('_', ' ')}: {match['title']}",
           "body": (f"{place['name']} matches an open {match['kind']} commitment "
                   f"('{match['title']}', due {match['due_date']}).")}


# ---------------------------------------------------------------------------
# 4. refuel / cadence
# ---------------------------------------------------------------------------

def _cadence_slack_days() -> int:
    return 2


@rule("cadence")
def _rule_cadence(ctx, place: dict) -> dict | None:
    fe = ctx.open_finance()
    try:
        from ..patterns import merchant_cadences
        cadences = merchant_cadences(fe)
    finally:
        fe.close()
    match = next((c for c in cadences if _fuzzy_hit(c["merchant"], place["name"])), None)
    if not match:
        return None
    last = _dt.date.fromisoformat(match["last_date"])
    days_since = (_dt.date.today() - last).days
    slack = match.get("tolerance_days", 0) + _cadence_slack_days()
    if days_since < match["gap_days"] + slack:
        return None
    return {"need_key": "cadence_overdue", "tier0_action": None,
           "title": f"Usual {place['name']} run overdue?",
           "body": (f"You usually visit ~every {match['gap_days']} days; it's been "
                   f"{days_since} days since the last one ({match['last_date']}).")}


# ---------------------------------------------------------------------------
# 5. spend_caution extend
# ---------------------------------------------------------------------------

_KIND_BUDGET_ALIASES = {
    "grocer": "food", "supermarket": "food", "restaurant": "food", "cafe": "food",
    "mall": "shopping", "bazaar": "shopping", "pharmac": "health",
    "fuel": "transport", "petrol": "transport",
}


def _spend_caution_pct() -> float:
    from .. import config
    try:
        return float(config._env("AMY_LIFE_SPEND_CAUTION_PCT", "85")) / 100.0
    except ValueError:
        return 0.85


@rule("spend_caution")
def _rule_spend_caution(ctx, place: dict) -> dict | None:
    kind_tokens = _tokens(place["kind"] + " " + place["name"])
    aliased = set()
    for tok in kind_tokens:
        for alias_key, cat in _KIND_BUDGET_ALIASES.items():
            if alias_key in tok:
                aliased.add(cat)
    if not aliased:
        return None
    fe = ctx.open_finance()
    try:
        statuses = fe.budget_status()
    finally:
        fe.close()
    pct = _spend_caution_pct()
    for b in statuses:
        if not b.get("limit"):
            continue
        if not (aliased & _tokens(b["category"])):
            continue
        ratio = (b.get("spent") or 0) / b["limit"]
        if ratio >= pct:
            return {"need_key": f"spend_caution_{b['category']}", "tier0_action": None,
                   "title": f"Heads up before you spend at {place['name']}",
                   "body": (f"Your {b['category']} budget is at {ratio*100:.0f}% "
                           f"({b.get('spent', 0):.0f}/{b['limit']:.0f}) this month.")}
    return None


# ---------------------------------------------------------------------------
# 6. cafe_habit
# ---------------------------------------------------------------------------

@rule("cafe_habit")
def _rule_cafe_habit(ctx, place: dict) -> dict | None:
    if not _merchant_matches(place["name"], _CAFE_TOKENS) and place["kind"] not in ("cafe", "restaurant"):
        return None
    links = [l for l in ctx.store.list_habit_links(ctx.user_id)
            if l["signal_type"] == "txn_absence"
            and _merchant_matches(" ".join(l["signal_params"].get("merchant_tokens") or []), _CAFE_TOKENS)]
    if not links:
        return None
    habits = ctx.open_habits()
    try:
        recent_misses = 0
        today = _dt.date.today()
        for i in range(7):
            d = (today - _dt.timedelta(days=i)).isoformat()
            row = habits.db.execute(
                "SELECT done FROM habit_logs WHERE habit_id=? AND date=?",
                (links[0]["habit_id"], d)).fetchone()
            if not row or not row["done"]:
                recent_misses += 1
    finally:
        habits.close()
    if recent_misses < 4:
        return None
    return {"need_key": "slipping_home_brew", "tier0_action": None,
           "title": "Slipping on home-brew?",
           "body": f"Missed your home-brew habit {recent_misses} of the last 7 days — you're at {place['name']}."}


# ---------------------------------------------------------------------------
# 7. subscription_brand
# ---------------------------------------------------------------------------

@rule("subscription_brand")
def _rule_subscription_brand(ctx, place: dict) -> dict | None:
    fe = ctx.open_finance()
    try:
        subs = fe.list_subscriptions(status="active")
    finally:
        fe.close()
    match = next((s for s in subs if _fuzzy_hit(s["name"], place["name"])), None)
    if not match:
        return None
    return {"need_key": f"sub_{match['id']}", "tier0_action": None,
           "title": f"Still using {match['name']}?",
           "body": (f"You're at {place['name']}, which matches your active subscription "
                   f"'{match['name']}' (~{match.get('monthly_cost', 0):.0f}/mo). Worth reviewing?")}


# ---------------------------------------------------------------------------
# 8. person_proximity
# ---------------------------------------------------------------------------

@rule("person_proximity")
def _rule_person_proximity(ctx, place: dict) -> dict | None:
    # No person<->place association exists anywhere in this codebase
    # (person_cadences tracks merchant/transfer names, not locations) —
    # this rule has no real signal to check today and honestly never
    # fires until a "tag this place as X's area" flow is built.
    return None


# ---------------------------------------------------------------------------
# 9. gym_prompt (the one tier-0 write — one-tap check)
# ---------------------------------------------------------------------------

def _typical_gym_hour(ctx, place_id: str) -> int | None:
    rows = ctx.collab.conn.execute(
        "SELECT entered_at FROM geo_visits WHERE place_id=?", (place_id,)).fetchall()
    hours = [int(r["entered_at"][11:13]) for r in rows if r["entered_at"] and len(r["entered_at"]) >= 13]
    if len(hours) < 3:
        return None
    import statistics
    return round(statistics.median(hours))


@rule("gym_prompt")
def _rule_gym_prompt(ctx, place: dict) -> dict | None:
    if place["kind"] != "gym":
        return None
    typical_hour = _typical_gym_hour(ctx, place["place_id"])
    if typical_hour is None:
        return None
    now_hour = _dt.datetime.now().hour
    if abs(now_hour - typical_hour) > 1:
        return None
    links = [l for l in ctx.store.list_habit_links(ctx.user_id)
            if l["signal_type"] == "geo_place_visit" and l["signal_params"].get("kind") == "gym"]
    if not links:
        return None
    habit_id = links[0]["habit_id"]
    habits = ctx.open_habits()
    try:
        today = _dt.date.today().isoformat()
        row = habits.db.execute(
            "SELECT done FROM habit_logs WHERE habit_id=? AND date=?", (habit_id, today)).fetchone()
    finally:
        habits.close()
    if row and row["done"]:
        return None
    return {"need_key": "gym_unchecked", "tier0_action": {
               "action_type": "complete_habit_check",
               "payload": {"habit_id": habit_id, "date": today, "note": "auto via gym_prompt (L9)"}},
           "title": "Workout checked in", "body": f"At the gym around your usual time ({typical_hour}:00)."}


# ---------------------------------------------------------------------------
# 10. office_gap
# ---------------------------------------------------------------------------

@rule("office_gap")
def _rule_office_gap(ctx, place: dict) -> dict | None:
    if place["kind"] != "office":
        return None
    from .. import tools
    from ..connectors.mcp_call import extract_list
    try:
        meetings = (tools.invoke(ctx, "meet_upcoming_meetings", {"hours": 4}, actor="agent") or {}).get("meetings", [])
    except Exception:
        return None
    if not meetings:
        return None
    now = _dt.datetime.now(_dt.timezone.utc)
    starts = []
    for m in meetings:
        try:
            starts.append(_dt.datetime.fromisoformat(str(m.get("start")).replace("Z", "+00:00")))
        except Exception:
            continue
    upcoming = sorted(s for s in starts if s > now)
    if not upcoming:
        return None
    gap_min = (upcoming[0] - now).total_seconds() / 60
    if gap_min < 45:
        return None
    try:
        tasks_result = tools.invoke(ctx, "plane_list_tasks", {}, actor="agent")
        tasks = extract_list(tasks_result)
    except Exception:
        tasks = []
    today_s = _dt.date.today().isoformat()
    due_today = [t for t in tasks if str(t.get("due_date") or t.get("target_date") or "")[:10] == today_s]
    if not due_today:
        return None
    task = due_today[0]
    return {"need_key": "office_gap_task", "tier0_action": None,
           "title": "Quick task window before your meeting",
           "body": (f"~{gap_min:.0f} min before your next meeting, and "
                   f"'{task.get('title') or task.get('name')}' is due today.")}


# ---------------------------------------------------------------------------
# 11. travel_mode
# ---------------------------------------------------------------------------

@rule("travel_mode")
def _rule_travel_mode(ctx, place: dict) -> dict | None:
    from .aggregator import _has_home_signal, _home_place_id, infer_home_cell
    from ..geo import GeoStore

    gs = GeoStore(ctx.collab)
    home_place_id = _home_place_id(gs)
    home_cell = infer_home_cell(gs)
    today = _dt.date.today().isoformat()
    if _has_home_signal(gs, today, home_place_id, home_cell):
        return None   # not away today
    yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    y_metrics = ctx.store.get_life_metrics(ctx.user_id, yesterday)
    if not (y_metrics and y_metrics.get("day_type") == "away"):
        return None   # only fire once travel is already established, not on a single day's noise

    fe = ctx.open_finance()
    try:
        from ..commitments import CommitmentEngine
        travel_commitments = [c for c in CommitmentEngine(fe).list("open")
                              if "travel" in (c.get("title") or "").lower()
                              or "flight" in (c.get("title") or "").lower()]
    finally:
        fe.close()
    jurisdictions = ctx._extras.get("jurisdictions") or ["india"]
    fx_line = ""
    try:
        from ..jurisdictions import PackError, load_pack
        pack = load_pack(jurisdictions[0])
        fx_line = f" Home currency: {pack.get('currency', '?')}."
    except PackError:
        pass
    commitments_line = (f" Open travel commitments: {len(travel_commitments)}."
                        if travel_commitments else "")
    return {"need_key": "away_briefing", "tier0_action": None,
           "title": "Travel mode",
           "body": (f"Looks like you're away from home — grace is on (streaks/baselines paused)."
                    f"{commitments_line}{fx_line}")}


# ---------------------------------------------------------------------------
# 12. custodial_bank
# ---------------------------------------------------------------------------

@rule("custodial_bank")
def _rule_custodial_bank(ctx, place: dict) -> dict | None:
    if place["kind"] != "bank":
        return None
    fe = ctx.open_finance()
    try:
        from ..finance.custodial import run_validation
        accounts = [a for a in fe.list_accounts() if a.get("account_type") == "custodial"]
        for acc in accounts:
            result = run_validation(fe, acc["id"])
            if result["issues"]:
                first = result["issues"][0]
                return {"need_key": f"custodial_{acc['id']}", "tier0_action": None,
                       "title": f"Custodial account needs attention: {acc.get('nickname', acc['id'])}",
                       "body": f"{len(result['issues'])} open issue(s) — e.g. {first['check']}."}
    finally:
        fe.close()
    return None
