"""Digital Twin — one composed, queryable model of the user.

Fuses the existing pieces into a single representation:
  - profile  (skills / projects / interests / goals)   ← product.profile
  - memory   (preferences, recent activity, frequent notes) ← collab.memory
  - goals    (active goals + progress)                  ← collab.planner
  - traits   (derived: focus areas, momentum, cadence)  ← computed here

snapshot() returns the model; ask() answers questions about the user from it.
"""
from __future__ import annotations

from ..product.profile import ProfileBuilder
from ..collab.memory import MemoryManager
from ..collab.planner import PlannerAgent
from ..collab.learning import LearningAgent


class DigitalTwin:
    def __init__(self, notes, collab_db, llm=None):
        self.notes = notes
        self.db = collab_db
        self.llm = llm
        self.profile = ProfileBuilder(notes, collab_db=collab_db)
        self.memory = MemoryManager(collab_db)
        self.planner = PlannerAgent(collab_db)
        self.learning = LearningAgent(collab_db, self.memory)

    # --- derived traits -----------------------------------------------------
    def traits(self) -> dict:
        domains = self.profile.interests()                      # ranked by note count
        trends = self.learning.trends()
        rising = [d for d, t in trends.items() if t["trend"] == "increasing"]
        goals = self.planner.list_goals()
        active = [g for g in goals if g["status"] == "active"]
        return {
            "focus_areas": domains[:5],
            "momentum": rising,                                 # what's trending up
            "active_goal_count": len(active),
            "completed_goal_count": len([g for g in goals if g["status"] == "done"]),
            "engagement": len(self.memory.recent_activities(50)),
        }

    # --- composed model -----------------------------------------------------
    def snapshot(self) -> dict:
        prof = self.profile.build()
        return {
            "profile": prof,
            "memory": self.memory.snapshot(),
            "goals": self.planner.list_goals(),
            "traits": self.traits(),
        }

    # --- query the twin -----------------------------------------------------
    def ask(self, question: str, llm=None) -> dict:
        model = self.snapshot()
        llm = llm or self.llm
        facts = (
            f"Skills: {', '.join(model['profile']['skills'][:12])}\n"
            f"Projects: {', '.join(p['title'] for p in model['profile']['projects'][:8])}\n"
            f"Interests/focus: {', '.join(model['traits']['focus_areas'])}\n"
            f"Momentum (rising): {', '.join(model['traits']['momentum']) or 'n/a'}\n"
            f"Active goals: {', '.join(g['title'] for g in model['goals'] if g['status']=='active') or 'none'}\n"
            f"Preferences: {model['memory']['preferences']}"
        )
        if llm is not None:
            answer, m = llm.generate(
                "You are the user's digital twin. Answer questions about THIS user using "
                "only the facts provided. Speak in third person about the user.",
                question, facts)
        else:
            answer, m = "Here's what I know about you:\n" + facts, "none"
        return {"question": question, "answer": answer, "model": m, "facts": model["traits"]}
