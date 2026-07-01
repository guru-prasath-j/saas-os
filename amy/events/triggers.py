"""Default triggers + digest builder.

Triggers are subscribers wired at startup so the system reacts to events
(e.g. completing a goal writes a memory note). The digest composes
reflection + learning + suggestions and is what a scheduler would run daily.
"""
from __future__ import annotations

from . import store


def register_default_triggers(events, memory):
    """Wire reactive behavior onto an EventStore."""
    def on_goal_completed(ev):
        title = ev["payload"].get("title", "a goal")
        memory.add_summary(f"🎉 Completed goal: {title}")

    def on_vault_imported(ev):
        n = ev["payload"].get("notes_loaded", "?")
        memory.add_summary(f"Imported vault ({n} notes).")

    events.subscribe(store.GOAL_COMPLETED, on_goal_completed)
    events.subscribe(store.VAULT_IMPORTED, on_vault_imported)


def build_digest(reflection, learning, planner, suggestions_fn, days: int = 7) -> dict:
    """Compose the proactive digest (what the scheduler emits)."""
    return {
        "reflection": reflection.weekly_summary(days),
        "trends": learning.trends(days),
        "recommendations": learning.recommendations(days),
        "suggestions": suggestions_fn(learning, reflection, planner, days)["suggestions"],
        "open_goals": [g for g in planner.list_goals() if g["status"] == "active"],
    }
