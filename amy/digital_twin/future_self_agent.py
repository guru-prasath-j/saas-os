"""FutureSelfAgent — validates decisions against long-term goals.

Given a proposed decision (title, category, optional reason), it checks the
decision for *alignment* with the user's active long-term goals and priorities,
then returns a verdict ("aligned" / "neutral" / "conflict") with reasoning and
a message phrased as advice from the user's future self.

Heuristic and transparent: it matches the decision's category/keywords against
goal domains and titles, and weighs the user's stated priorities.
"""
from __future__ import annotations

import re

from ..engines.predictive_engine import PredictiveEngine

# light category -> related goal-domain hints
_CATEGORY_DOMAINS = {
    "career": {"career", "work", "job", "professional"},
    "finance": {"finance", "money", "financial", "budget"},
    "health": {"health", "fitness", "wellness"},
    "learning": {"learning", "education", "study", "skill"},
    "projects": {"projects", "project", "build", "side"},
    "personal": {"personal", "life", "family", "relationships"},
}


def _tokens(text: str) -> set[str]:
    return set(w for w in re.findall(r"[a-z']+", (text or "").lower()) if len(w) > 2)


class FutureSelfAgent:
    def __init__(self, collab_db, priorities=None):
        self.db = collab_db.conn
        self.collab_db = collab_db
        self.priorities = priorities or []

    def _active_goals(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, title, domain, progress, target_date FROM goals WHERE status!='done'"
        ).fetchall()
        return [dict(r) for r in rows]

    def validate(self, title: str, category: str = "personal", reason: str = "") -> dict:
        goals = self._active_goals()
        dec_tokens = _tokens(f"{title} {reason}")
        related_domains = _CATEGORY_DOMAINS.get(category, set())

        supporting = []
        conflicting = []
        for g in goals:
            gdom = (g.get("domain") or "").lower()
            gtok = _tokens(g.get("title", ""))
            overlap = dec_tokens & gtok
            domain_match = gdom in related_domains or gdom == category
            if domain_match or overlap:
                supporting.append({"goal": g["title"], "why": (
                    f"shares domain '{gdom}'" if domain_match else
                    f"keyword overlap: {', '.join(sorted(overlap))}")})

        # very light conflict signal: negative intent words against a goal domain
        neg = {"quit", "stop", "drop", "abandon", "cancel", "delay", "pause"}
        if dec_tokens & neg:
            for g in goals:
                gdom = (g.get("domain") or "").lower()
                if gdom in related_domains or gdom == category:
                    conflicting.append({"goal": g["title"],
                                        "why": "decision may abandon/delay this goal"})

        if conflicting:
            verdict = "conflict"
        elif supporting:
            verdict = "aligned"
        else:
            verdict = "neutral"

        # priority alignment
        prio_hit = category in self.priorities or any(
            p in dec_tokens for p in self.priorities)

        message = self._message(verdict, category, supporting, conflicting, prio_hit)
        return {
            "decision": title,
            "category": category,
            "verdict": verdict,
            "supports": supporting,
            "conflicts": conflicting,
            "aligns_with_priorities": prio_hit,
            "active_goals_considered": len(goals),
            "future_self_says": message,
        }

    @staticmethod
    def _message(verdict, category, supporting, conflicting, prio_hit) -> str:
        if verdict == "conflict":
            names = ", ".join(c["goal"] for c in conflicting)
            return (f"Think twice — your future self is still working toward {names}. "
                    f"This {category} decision could set that back.")
        if verdict == "aligned":
            names = ", ".join(s["goal"] for s in supporting[:2])
            tail = " It also matches your stated priorities." if prio_hit else ""
            return (f"Go for it — this moves you toward {names}.{tail} "
                    f"Future you will likely thank you.")
        tail = " It's within your priorities, so it's reasonable." if prio_hit else \
               " It doesn't clearly advance your current goals — make sure it's worth the time."
        return f"Neutral on goals.{tail}"
