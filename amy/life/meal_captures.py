"""LIFE AUTOPILOT L8 — meal capture classification.

Food-tagged captures (incl. source='meta-glasses') get a SECOND,
sensitive=True classification pass over their ALREADY-extracted
caption/OCR/tags TEXT — never the image itself. captures.py's vision
call at ingest time is unavoidably cloud-based today (existing behavior,
out of scope to change here); this module's additional meal-specific
step works off the already-produced text description and stays local
(Ollama-only), satisfying the privacy floor for the NEW classification
this part adds.

Populates life_metrics.meal_captures (count of food captures that day)
and meal_calorie_est (estimate-labeled, NULL on low confidence — never
fabricates a number the classifier didn't actually produce).
"""
from __future__ import annotations

import json

_MEAL_PARSE_SYSTEM = (
    "You classify a photo's caption/OCR/tags for a personal food-log "
    "assistant. Given the text below, decide if this looks like a photo "
    "OF FOOD being eaten (a meal or snack) and, only if so, give a rough "
    "calorie ESTIMATE (a single number, your best midpoint guess — return "
    "null if you truly cannot estimate). Return STRICT JSON only: "
    '{"is_meal": true|false, "calorie_estimate": <number or null>}'
)


def classify_capture(record: dict, llm) -> dict | None:
    """record: a captures.py record dict (caption/ocr/tags/place/...).
    Returns {is_meal, calorie_estimate} or None on no signal/LLM/parse
    failure — never a guess presented as a fact."""
    if llm is None:
        return None
    caption = record.get("caption") or ""
    ocr = record.get("ocr") or ""
    tags = record.get("tags") or []
    text = f"Caption: {caption}\nOCR: {ocr}\nTags: {', '.join(tags)}".strip()
    if not (caption or ocr or tags):
        return None
    try:
        raw, _model = llm.generate(_MEAL_PARSE_SYSTEM, text, sensitive=True)
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception:
        return None
    if not data.get("is_meal"):
        return None
    cal = data.get("calorie_estimate")
    calorie_estimate = float(cal) if isinstance(cal, (int, float)) and cal > 0 else None
    return {"is_meal": True, "calorie_estimate": calorie_estimate}


def day_meal_signals(ctx, date: str, llm) -> dict:
    """One classification call per capture that day (a real, documented
    cost — gated by AMY_AGENT_LIFE_CAPTURE_MEALS so it's opt-out-able).
    Returns {meal_captures, meal_calorie_est}."""
    from .. import captures as captures_mod
    from ..saas import tenancy

    try:
        vault = tenancy.resolve_vault_dir(ctx.user_id)
        recs = captures_mod.captures_between(date, date, vault=vault)
    except Exception:
        recs = []

    count = 0
    total_cal = 0.0
    any_cal = False
    for r in recs:
        result = classify_capture(r, llm)
        if not result:
            continue
        count += 1
        if result["calorie_estimate"] is not None:
            total_cal += result["calorie_estimate"]
            any_cal = True
    return {"meal_captures": count,
           "meal_calorie_est": round(total_cal, 0) if any_cal else None}


def _get_llm(ctx):
    if ctx.llm is not None:
        return ctx.llm
    cached = ctx._extras.get("lazy_llm")
    if cached is not None:
        return cached
    try:
        from ..llm import LLMRouter
        from ..automation.store import TrackedLLM
        llm = TrackedLLM(LLMRouter(use_global_keys=True), ctx.store,
                         purpose="life_meal_captures")
    except Exception:
        llm = None
    ctx._extras["lazy_llm"] = llm
    return llm
