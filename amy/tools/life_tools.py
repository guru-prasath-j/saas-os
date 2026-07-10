"""LIFE AUTOPILOT registry tools.

health_targets (L1) is read-only: it computes from whatever is on file in
health_profile, honestly returning available=False (never a fabricated
number) when the profile is incomplete — same "honest stub" idiom
career_apply.py's company intel uses for a missing connector.

complete_habit_check / adjust_habit_target (L4) are registered here for
human/chat-assistant use — a human clicking "mark done" or an agent
proposing a target change via the generic tool-call path. The habit_links
AUTO-completion mechanism (amy/life/habits.py) never calls these through
the registry — it calls submit_action() directly, which is how it gets
tier 0/1 instead of AGENT_GATE's forced tier 2 for actor='agent' writes.
"""
from __future__ import annotations

from .registry import RISK_READ, RISK_WRITE, register_tool


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


@register_tool("health_targets",
               "Current health targets (calorie budget, sleep window, "
               "protein, water) computed from the user's health profile via "
               "Mifflin-St Jeor + activity multiplier / age-band sleep "
               "formulas. Estimates, not medical advice — always shows the "
               "formula. available=False (no numbers) when the health "
               "profile is missing essentials.",
               _obj({}),
               RISK_READ)
def _t_health_targets(ctx, args):
    from ..life import targets as life_targets
    from ..life.bootstrap import missing_essentials

    profile = ctx.store.get_health_profile(ctx.user_id)
    if not profile:
        return {"available": False, "reason": "no health profile on file"}
    missing = missing_essentials(profile)
    if missing:
        return {"available": False, "reason": "health profile incomplete",
               "missing": missing}
    age = life_targets.resolve_age(profile.get("dob_or_age") or "")
    computed = life_targets.all_targets(
        profile.get("sex") or "", float(profile["weight_kg"]),
        float(profile["height_cm"]), age, profile.get("activity_level") or "")
    return {"available": True, "computed": computed,
           "accepted": profile.get("targets") or {}}


@register_tool("propose_habit",
               "Propose a new habit, optionally linked to an auto-tracking "
               "signal (e.g. geo_place_visit for a gym habit). Always tier "
               "2 with evidence when invoked by an agent.",
               _obj({"title": {"type": "string"}, "frequency": {"type": "string"},
                     "link": {"type": "object"}, "reasoning": {"type": "string"}},
                    ["title", "reasoning"]),
               RISK_WRITE)
def _t_propose_habit(ctx, args):
    from ..automation.executors import submit_action
    return submit_action(
        ctx, 2, "propose_habit", title=f"Proposed habit: {args['title']}",
        body=args["reasoning"],
        payload={"title": args["title"], "frequency": args.get("frequency", "daily"),
                "link": args.get("link")},
        source="manual", reasoning=args["reasoning"], risk="write")


@register_tool("propose_goal",
               "Propose a new goal. Always tier 2 with evidence when "
               "invoked by an agent.",
               _obj({"title": {"type": "string"}, "domain": {"type": "string"},
                     "target_date": {"type": "string"}, "reasoning": {"type": "string"}},
                    ["title", "reasoning"]),
               RISK_WRITE)
def _t_propose_goal(ctx, args):
    from ..automation.executors import submit_action
    return submit_action(
        ctx, 2, "propose_goal", title=f"Proposed goal: {args['title']}",
        body=args["reasoning"],
        payload={"title": args["title"], "domain": args.get("domain", "life"),
                "target_date": args.get("target_date")},
        source="manual", reasoning=args["reasoning"], risk="write")


@register_tool("complete_habit_check",
               "Mark a habit done for a date (default today). Human/chat "
               "use — the auto-completion mechanism (habit_links) never "
               "calls this tool; it calls the executor directly for tier "
               "0/1 auto-completion.",
               _obj({"habit_id": {"type": "string"},
                     "date": {"type": "string"},
                     "note": {"type": "string"}}, ["habit_id"]),
               RISK_WRITE)
def _t_complete_habit_check(ctx, args):
    from ..automation.executors import execute
    return execute(ctx, "complete_habit_check",
                   {"habit_id": args["habit_id"], "date": args.get("date"),
                    "note": args.get("note", "")})


@register_tool("adjust_habit_target",
               "Propose a change to a habit's grace-per-week tolerance "
               "(the only 'target' this system enforces — frequency is a "
               "display label only). Always tier 2 with an old->new diff "
               "when invoked by an agent.",
               _obj({"habit_id": {"type": "string"},
                     "new_grace_per_week": {"type": "integer"}},
                    ["habit_id", "new_grace_per_week"]),
               RISK_WRITE)
def _t_adjust_habit_target(ctx, args):
    from ..life.habits import effective_grace_per_week
    from ..automation.executors import submit_action
    habit_id = args["habit_id"]
    current = effective_grace_per_week(ctx, habit_id)
    new_grace = int(args["new_grace_per_week"])
    return submit_action(
        ctx, 2, "adjust_habit_target",
        title=f"Adjust grace-per-week for habit {habit_id}",
        body=f"Grace-per-week: {current} -> {new_grace}.",
        payload={"habit_id": habit_id, "old_grace_per_week": current,
                "new_grace_per_week": new_grace},
        source="manual", reasoning="Manually requested target adjustment.",
        risk="write", affected_entity=f"habit={habit_id}")
