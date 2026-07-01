"""Reflection Agent — periodic (e.g. weekly) summary: Progress, Gaps, Suggestions.

Built from the activity log + goal progress. LLM optional (narrative); otherwise a
structured heuristic summary.
"""
from __future__ import annotations

import datetime as _dt
from collections import Counter


class ReflectionAgent:
    def __init__(self, db, planner, memory, llm=None):
        self.db = db.conn
        self.planner = planner
        self.memory = memory
        self.llm = llm

    def _since(self, days: int) -> str:
        return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).isoformat()

    def weekly_summary(self, days: int = 7) -> dict:
        since = self._since(days)
        acts = self.db.execute(
            "SELECT kind, domain FROM activities WHERE ts>=?", (since,)).fetchall()
        by_domain = Counter(a["domain"] for a in acts if a["domain"])
        n_queries = sum(1 for a in acts if a["kind"] == "query")

        goals = self.planner.list_goals()
        advanced = [g for g in goals if (g["progress"] or 0) > 0 and g["status"] == "active"]
        done = [g for g in goals if g["status"] == "done"]
        stalled = [g for g in goals if (g["progress"] or 0) == 0 and g["status"] == "active"]

        progress = []
        if n_queries:
            progress.append(f"{n_queries} questions across {len(by_domain)} domains "
                            f"({', '.join(f'{d}:{c}' for d, c in by_domain.most_common())}).")
        for g in done:
            progress.append(f"Completed goal: {g['title']}.")
        for g in advanced:
            progress.append(f"Advanced '{g['title']}' to {g['progress']}%.")

        gaps = []
        for g in stalled:
            gaps.append(f"No progress on '{g['title']}'.")
        if not goals:
            gaps.append("No goals set yet.")

        suggestions = []
        for g in stalled[:3]:
            suggestions.append(f"Add a first milestone for '{g['title']}' to get unblocked.")
        cold = [d for d in ("finance", "career", "health", "learning")
                if d not in by_domain]
        if cold:
            suggestions.append(f"You haven't touched {', '.join(cold)} this week — worth a check-in.")
        if not goals:
            suggestions.append("Create a goal so progress can be tracked.")

        result = {"period_days": days, "progress": progress or ["Quiet week — little activity logged."],
                  "gaps": gaps or ["No obvious gaps."], "suggestions": suggestions or ["Keep going."]}

        if self.llm is not None:
            ctx = (f"Progress: {result['progress']}\nGaps: {result['gaps']}\n"
                   f"Suggestions: {result['suggestions']}")
            narrative, _ = self.llm.generate(
                "Write a short, encouraging weekly reflection from these bullet points.",
                "Weekly reflection:", ctx)
            result["narrative"] = narrative
        return result
