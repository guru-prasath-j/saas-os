"""Context Engine — understands the user's current *mode* and reprioritizes
agents, goals, and recommendations accordingly.

Modes: work, learning, weekend, vacation, meeting, focus.
Detection is heuristic (time-of-day / weekend) + an explicit override the user
sets; reprioritization builds on the Executive Agent (no rewrite).
"""
from __future__ import annotations

import datetime as _dt

MODES = {
    "work":     {"domains": ["career", "projects", "finance"], "focus": "ship work + career goals"},
    "learning": {"domains": ["learning", "knowledge"],          "focus": "study and skill growth"},
    "weekend":  {"domains": ["family", "health", "personal"],   "focus": "rest, family, health"},
    "vacation": {"domains": ["family", "health"],               "focus": "disconnect — minimal work"},
    "meeting":  {"domains": ["career", "projects"],             "focus": "meeting prep + follow-ups"},
    "focus":    {"domains": [],                                  "focus": "single deep-work goal, no noise"},
}


class ContextEngine:
    def __init__(self, collab_db, llm=None):
        self.db = collab_db.conn
        from ..collab.memory import MemoryManager
        from ..autonomous.executive import ExecutiveAgent
        self.memory = MemoryManager(collab_db)
        self.exec = ExecutiveAgent(collab_db, llm)

    # --- mode -------------------------------------------------------------
    def set_mode(self, mode: str):
        if mode not in MODES:
            raise ValueError(f"unknown mode '{mode}'")
        self.memory.set_pref("mode", mode)

    def clear_mode(self):
        self.memory.set_pref("mode", "")

    def detect(self, now: _dt.datetime | None = None) -> str:
        """Explicit override wins; otherwise infer from the clock."""
        pref = self.memory.get_pref("mode")
        if pref:
            return pref
        now = now or _dt.datetime.now()
        if now.weekday() >= 5:
            return "weekend"
        if 9 <= now.hour < 18:
            return "work"
        return "focus"

    # --- reprioritize -------------------------------------------------------
    def profile(self, mode: str | None = None, events=None, now=None) -> dict:
        mode = mode or self.detect(now)
        cfg = MODES.get(mode, MODES["work"])
        mode_domains = set(cfg["domains"])

        # domains: executive order, with mode domains boosted to the front
        domain_order = self.exec.reprioritize_domains()
        domains = sorted(domain_order,
                         key=lambda d: (0 if d["domain"] in mode_domains else 1, -d["score"]))
        # goals: prioritized, with mode-domain goals first
        goals = self.exec.prioritize_goals()
        goals.sort(key=lambda g: (0 if g["domain"] in mode_domains else 1, -g["priority"]))

        recommendations = []
        if mode == "focus":
            recommendations.append("Work only the top goal; mute other agents.")
        elif mode in ("weekend", "vacation"):
            recommendations.append("Deprioritize work/finance; surface family & health.")
        elif mode == "meeting":
            recommendations.append("Surface relevant notes and action items for upcoming meetings.")
        if goals:
            recommendations.append(f"Next: {goals[0]['title']}")

        if events is not None:
            try:
                events.emit("context.mode", {"mode": mode}, source="context")
            except Exception:
                pass

        return {
            "mode": mode,
            "focus": cfg["focus"],
            "priority_domains": [d["domain"] for d in domains],
            "suggested_agents": [f"{d}_agent" for d in cfg["domains"]],
            "top_goals": goals[:5],
            "recommendations": recommendations,
        }
