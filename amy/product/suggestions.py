"""Proactive Suggestions — recommendations generated without the user asking.

Fuses: learning trends, reflection gaps/suggestions, and stalled goals.
"""
from __future__ import annotations


def build_suggestions(learning, reflection, planner, window_days: int = 7) -> dict:
    items = []

    # 1) learning trends
    items += [{"source": "learning", "text": r} for r in learning.recommendations(window_days)]

    # 2) reflection gaps -> suggestions
    refl = reflection.weekly_summary(window_days)
    items += [{"source": "reflection", "text": s} for s in refl.get("suggestions", [])]

    # 3) stalled goals
    for g in planner.list_goals():
        if g["status"] == "active" and (g["progress"] or 0) == 0:
            items.append({"source": "planner",
                          "text": f"Goal '{g['title']}' has no progress — add a first milestone."})

    # de-duplicate by text, keep order
    seen, out = set(), []
    for it in items:
        if it["text"] not in seen:
            seen.add(it["text"])
            out.append(it)
    return {"suggestions": out, "count": len(out)}
