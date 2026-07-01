"""DigitalTwinEngine — the composed model of the user.

Builds on the existing ``amy.twin.DigitalTwin`` (profile + memory + goals +
traits) and extends it with the two data sources the spec calls for but the
original twin lacked: **habits** and **decisions**. It also folds in the
PersonalityEngine so the twin can speak/act in the user's style.

Data sources fused:
  Vault / Profile · Memory · Goals · Habits · Decisions · Personality

This engine is additive — ``amy.twin.DigitalTwin`` is unchanged.
"""
from __future__ import annotations

from ..twin.twin import DigitalTwin
from ..engines.decision_engine import DecisionEngine
from .personality_engine import PersonalityEngine


class DigitalTwinEngine:
    def __init__(self, notes, collab_db, llm=None):
        self.notes = notes
        self.db = collab_db
        self.llm = llm
        self.twin = DigitalTwin(notes, collab_db, llm=llm)
        self.decisions = DecisionEngine(collab_db)
        self.personality = PersonalityEngine(notes, collab_db)

    # --- habits ---------------------------------------------------------
    def habits(self) -> dict:
        """Derive behavioural habits from activity cadence + learning trends."""
        mem = self.twin.memory
        acts = mem.recent_activities(200)
        # cadence by kind
        by_kind: dict[str, int] = {}
        by_domain: dict[str, int] = {}
        for a in acts:
            by_kind[a.get("kind", "?")] = by_kind.get(a.get("kind", "?"), 0) + 1
            d = a.get("domain") or "general"
            by_domain[d] = by_domain.get(d, 0) + 1
        top_kinds = sorted(by_kind.items(), key=lambda x: -x[1])[:5]
        top_domains = sorted(by_domain.items(), key=lambda x: -x[1])[:5]
        trends = self.twin.learning.trends()
        consistent = [d for d, t in trends.items() if t["trend"] in ("increasing", "steady")]
        return {
            "frequent_actions": [k for k, _ in top_kinds],
            "frequent_domains": [d for d, _ in top_domains],
            "consistent_areas": consistent,
            "activity_volume": len(acts),
        }

    # --- decisions ------------------------------------------------------
    def decision_profile(self) -> dict:
        a = self.decisions.analyze()
        return {
            "total": a["total"],
            "resolution_rate": a["resolution_rate"],
            "success_rate": a["success_rate"],
            "avg_confidence": a["avg_confidence"],
            "strong_categories": [c for c, v in a["by_category"].items()
                                  if (v.get("success_rate") or 0) >= 0.6],
            "weak_categories": [c for c, v in a["by_category"].items()
                                if v.get("success_rate") is not None and v["success_rate"] < 0.4],
        }

    # --- full snapshot --------------------------------------------------
    def snapshot(self) -> dict:
        base = self.twin.snapshot()  # profile, memory, goals, traits
        base["habits"] = self.habits()
        base["decisions"] = self.decision_profile()
        base["personality"] = self.personality.profile()
        return base

    # --- speak as the user ----------------------------------------------
    def ask(self, question: str, llm=None) -> dict:
        """Answer as the user's twin, now informed by habits, decisions & style."""
        model = self.snapshot()
        llm = llm or self.llm
        p = model["personality"]
        facts = (
            f"Skills: {', '.join(model['profile']['skills'][:12])}\n"
            f"Focus areas: {', '.join(model['traits']['focus_areas'])}\n"
            f"Habits: actions={model['habits']['frequent_actions']}, "
            f"domains={model['habits']['frequent_domains']}\n"
            f"Decision style: success_rate={model['decisions']['success_rate']}, "
            f"avg_confidence={model['decisions']['avg_confidence']}\n"
            f"Writing style: {p['writing_style']}\n"
            f"Priorities: {', '.join(p['priorities'])}\n"
            f"Active goals: {', '.join(g['title'] for g in model['goals'] if g['status']=='active') or 'none'}"
        )
        if llm is not None:
            answer, m = llm.generate(
                "You ARE the user's digital twin. Answer in the user's own voice and "
                "style using only the facts provided. Reflect their habits, priorities "
                "and decision tendencies.", question, facts)
        else:
            answer, m = "Based on your model:\n" + facts, "none"
        return {"question": question, "answer": answer, "model": m,
                "personality": p}
