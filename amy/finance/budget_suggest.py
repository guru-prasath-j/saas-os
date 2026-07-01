"""Suggest monthly budget caps per category from income + spend + location.

Works even with zero transaction history (pure income-based split) and gets
sharper once real category spend exists for the month — spend anchors the
suggestion so it isn't just generic percentages. A single LLM call localizes
the split to the user's cost-of-living (a flat with rent, or free-food config
in India looks very different from most of, say, Germany) and rounds to
sensible amounts; a rule-based percentage split is the fallback if the LLM
is unavailable.
"""
from __future__ import annotations

import json
import re

# Baseline % of income if there's no spend data to anchor on yet. Leaves ~15%
# unallocated for savings/misc rather than budgeting 100% of income away.
_DEFAULT_SPLIT: dict[str, float] = {
    "Rent": 0.30, "Food": 0.15, "Transport": 0.10, "Utilities": 0.05,
    "Health": 0.05, "Entertainment": 0.05, "Shopping": 0.05,
    "Education": 0.05, "Insurance": 0.05,
}

_SYSTEM = (
    "You are a budgeting assistant. Given a user's monthly income, location "
    "(cost-of-living context), and their actual spend this month per category "
    "(if any), propose a sensible monthly budget cap per category. Prefer "
    "round numbers. If a category already has real spend, don't set the cap "
    "far below it. Total across all categories should leave some of the "
    'income unallocated for savings. Return ONLY a JSON array: '
    '[{"category":"Food","limit":8000}, ...].'
)


def suggest_budgets(engine, location: str | None, llm) -> dict:
    """Returns {"income": float, "suggestions": [...]} or a "reason" if income is 0."""
    income = engine.effective_monthly_income()
    if income <= 0:
        return {"income": 0, "suggestions": [],
                "reason": "No income recorded yet — add an income source or "
                          "wait for salary transactions before requesting budgets."}

    spend = engine.this_month_spend()
    existing = {b["category"] for b in engine.list_budgets()}
    categories = sorted((set(_DEFAULT_SPLIT) | set(spend)) - existing)
    if not categories:
        return {"income": income, "suggestions": []}

    fallback = [
        {"category": cat,
         "limit": round(max(spend.get(cat, 0.0), income * _DEFAULT_SPLIT.get(cat, 0.03)), 0),
         "current_spend": round(spend.get(cat, 0.0), 2)}
        for cat in categories
    ]

    if llm is None:
        return {"income": income, "suggestions": fallback}

    lines = "\n".join(
        f'{c["category"]}: current spend this month ₹{c["current_spend"]:.0f}'
        for c in fallback
    )
    prompt = (
        f"Monthly income: ₹{income:.0f}\n"
        f"Location: {location or 'unspecified — assume India'}\n"
        f"Categories:\n{lines}"
    )
    try:
        raw_resp, _ = llm.generate(_SYSTEM, prompt, sensitive=False)
        raw_resp = re.sub(r"```(?:json)?", "", raw_resp).strip()
        start, end = raw_resp.find("["), raw_resp.rfind("]")
        if start != -1 and end != -1:
            items = json.loads(raw_resp[start:end + 1])
            by_cat = {c["category"]: c for c in fallback}
            suggestions = []
            for item in items:
                cat = item.get("category")
                limit = item.get("limit")
                if cat not in by_cat or not isinstance(limit, (int, float)) or limit <= 0:
                    continue
                suggestions.append({
                    "category": cat, "limit": round(limit, 0),
                    "current_spend": by_cat[cat]["current_spend"],
                })
            if suggestions:
                return {"income": income, "suggestions": suggestions}
    except Exception:
        pass  # degrade to rule-only fallback below

    return {"income": income, "suggestions": fallback}
