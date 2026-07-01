"""Agent Dashboard — assembles the product overview:
active agents, managed notes, confidence score, relationships, memory count.
"""
from __future__ import annotations

from ..pkos import domains as domainmod
from .marketplace import Marketplace


def build_dashboard(notes, collab_db, knowledge=None, finance_db=None) -> dict:
    dm = domainmod.detect(notes)
    mk = Marketplace(collab_db)

    agents = [{"agent": f"{d}_agent", "domain": d, "notes": len(p),
               "enabled": mk.is_enabled(f"{d}_agent")} for d, p in sorted(dm.items())]
    active = [a for a in agents if a["enabled"]]

    c = collab_db.conn
    memory = {
        "activities": c.execute("SELECT COUNT(*) n FROM activities").fetchone()["n"],
        "summaries": c.execute("SELECT COUNT(*) n FROM summaries").fetchone()["n"],
        "accessed_notes": c.execute("SELECT COUNT(*) n FROM note_access").fetchone()["n"],
        "goals": c.execute("SELECT COUNT(*) n FROM goals").fetchone()["n"],
    }
    memory["total"] = memory["activities"] + memory["summaries"] + memory["goals"]

    relationships = 0
    confidence = None
    managed = len(notes)
    if knowledge is not None:
        relationships = len(knowledge.relationships.graph())
        metas = knowledge.metadata.all()
        if metas:
            managed = len(metas)
            confidence = round(sum(m["importance"] for m in metas) / len(metas), 1)

    finance: dict = {}
    if finance_db is not None:
        try:
            ov = finance_db.overview()
            top_cats = sorted(ov["this_month_spend"].items(), key=lambda x: -x[1])[:3]
            over_budget = [b["category"] for b in ov["budget_status"] if b["over_budget"]]
            finance = {
                "balance_estimate": ov["balance_estimate"],
                "monthly_income": ov["monthly_income"],
                "subscription_monthly_total": ov["subscription_monthly_total"],
                "upcoming_bills_count": len(ov["upcoming_bills"]),
                "over_budget_categories": over_budget,
                "portfolio_value": ov["portfolio"]["total_value"],
                "top_spend_categories": [{"category": c, "amount": a} for c, a in top_cats],
            }
        except Exception:
            pass

    return {
        "active_agents": active,
        "all_agents": agents,
        "agent_count": len(active),
        "managed_notes": managed,
        "confidence_score": confidence,   # avg note importance (0-100) when knowledge built
        "relationships": relationships,
        "memory_count": memory["total"],
        "memory": memory,
        "finance": finance,
    }
