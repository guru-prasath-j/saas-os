"""Deterministic health-target math (LIFE AUTOPILOT L1).

Pure functions, no I/O, no LLM. Every target returns
{value, unit, formula, inputs} so the caller (bootstrap.py, life_tools.py)
can always show the reader exactly how a number was produced — the
"estimates, not medical advice" hard rule. Nothing here is a diagnosis;
these are the standard Mifflin-St Jeor / activity-multiplier / age-band
formulas, not a personalized medical assessment.

ESTIMATE DISCLAIMER (attach verbatim wherever a target from this module is
shown): "Estimate from standard formulas (Mifflin-St Jeor + activity
multiplier / age-band sleep) — not medical advice. Adjust or dismiss any
proposal that doesn't fit you."
"""
from __future__ import annotations

import datetime as _dt
import re as _re

ESTIMATE_DISCLAIMER = (
    "Estimate from standard formulas (Mifflin-St Jeor + activity multiplier "
    "/ age-band sleep) — not medical advice. Adjust or dismiss any "
    "proposal that doesn't fit you."
)

# Harris-Benedict-style activity multipliers applied to BMR for TDEE.
_ACTIVITY_MULTIPLIERS = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "active": 1.725,
    "very_active": 1.9,
}

# Protein g/kg by activity level — conservative end of the commonly cited
# 0.8-2.2 g/kg range, not a bodybuilding target.
_PROTEIN_G_PER_KG = {
    "sedentary": 0.8,
    "light": 1.0,
    "moderate": 1.4,
    "active": 1.6,
    "very_active": 1.8,
}

_WATER_ML_PER_KG = 33.0


def resolve_age(dob_or_age: str) -> int | None:
    """health_profile.dob_or_age accepts either an ISO date-of-birth
    ('1990-04-12') or a plain integer age ('34') — whichever the user/vault
    note provided. Returns None (never fabricates) if neither parses."""
    v = (dob_or_age or "").strip()
    if not v:
        return None
    if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        try:
            dob = _dt.date.fromisoformat(v)
        except ValueError:
            return None
        today = _dt.date.today()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    if _re.fullmatch(r"\d{1,3}", v):
        age = int(v)
        return age if 0 < age < 130 else None
    return None


def _norm_activity(activity_level: str) -> str:
    a = (activity_level or "").strip().lower()
    return a if a in _ACTIVITY_MULTIPLIERS else "sedentary"


def bmr_mifflin_st_jeor(sex: str, weight_kg: float, height_cm: float, age: int) -> dict:
    """Mifflin-St Jeor BMR (kcal/day).
    Male:   10*weight + 6.25*height - 5*age + 5
    Female: 10*weight + 6.25*height - 5*age - 161
    Any other/unspecified sex: average of the two sex offsets (+5 and -161
    average to -78) — an honest approximation, not a third formula."""
    s = (sex or "").strip().lower()
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    if s in ("male", "m"):
        value = base + 5
        formula = "10×weight_kg + 6.25×height_cm − 5×age + 5 (male)"
    elif s in ("female", "f"):
        value = base - 161
        formula = "10×weight_kg + 6.25×height_cm − 5×age − 161 (female)"
    else:
        value = base + (5 + -161) / 2
        formula = ("10×weight_kg + 6.25×height_cm − 5×age + (average of "
                   "male/female offset, sex not male/female)")
    return {"value": round(value, 1), "unit": "kcal/day", "formula": formula,
           "inputs": {"sex": sex, "weight_kg": weight_kg,
                      "height_cm": height_cm, "age": age}}


def tdee(bmr_value: float, activity_level: str) -> dict:
    """Total daily energy expenditure = BMR × activity multiplier."""
    level = _norm_activity(activity_level)
    mult = _ACTIVITY_MULTIPLIERS[level]
    value = bmr_value * mult
    return {"value": round(value, 1), "unit": "kcal/day",
           "formula": f"BMR × {mult} ({level} activity multiplier)",
           "inputs": {"bmr": bmr_value, "activity_level": level}}


def sleep_band(age: int) -> dict:
    """Age-band sleep-duration recommendation (published guideline bands,
    not personalized). <18: 8-10h; 18-64: 7-9h; 65+: 7-8h."""
    if age < 18:
        lo, hi, band = 8, 10, "under 18"
    elif age < 65:
        lo, hi, band = 7, 9, "18-64"
    else:
        lo, hi, band = 7, 8, "65+"
    return {"value": {"min_hours": lo, "max_hours": hi}, "unit": "hours/night",
           "formula": f"published age-band guideline ({band})",
           "inputs": {"age": age}}


def protein_target_g(weight_kg: float, activity_level: str) -> dict:
    level = _norm_activity(activity_level)
    per_kg = _PROTEIN_G_PER_KG[level]
    value = weight_kg * per_kg
    return {"value": round(value, 1), "unit": "g/day",
           "formula": f"{per_kg}g × weight_kg ({level} activity)",
           "inputs": {"weight_kg": weight_kg, "activity_level": level}}


def water_target_ml(weight_kg: float) -> dict:
    value = weight_kg * _WATER_ML_PER_KG
    return {"value": round(value), "unit": "ml/day",
           "formula": f"{_WATER_ML_PER_KG}ml × weight_kg",
           "inputs": {"weight_kg": weight_kg}}


def all_targets(sex: str, weight_kg: float, height_cm: float, age: int,
                activity_level: str) -> dict:
    """Every target computed from one profile, each self-describing.
    Returns {bmr, tdee, sleep, protein, water, disclaimer}."""
    bmr = bmr_mifflin_st_jeor(sex, weight_kg, height_cm, age)
    return {
        "bmr": bmr,
        "tdee": tdee(bmr["value"], activity_level),
        "sleep": sleep_band(age),
        "protein": protein_target_g(weight_kg, activity_level),
        "water": water_target_ml(weight_kg),
        "disclaimer": ESTIMATE_DISCLAIMER,
    }
