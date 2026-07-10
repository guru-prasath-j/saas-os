"""LIFE AUTOPILOT registry tools (L1: health_targets).

health_targets is read-only: it computes from whatever is on file in
health_profile, honestly returning available=False (never a fabricated
number) when the profile is incomplete — same "honest stub" idiom
career_apply.py's company intel uses for a missing connector.
"""
from __future__ import annotations

from .registry import RISK_READ, register_tool


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
