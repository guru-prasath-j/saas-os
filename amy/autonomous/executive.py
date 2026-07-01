"""Executive Agent (PIOS v2).

The "chief of staff": looks across all goals + domains and decides what matters.
  - prioritize_goals():    rank active goals (unblocked + progress + has next steps)
  - resolve_conflicts():   blocked chains, domain contention, overdue goals
  - coordinate_agents():   map top goals -> which domain agent should run
  - reprioritize_domains():order domains by goal priority + learning trends

Heuristic + deterministic (no LLM required). Reuses GoalEngine, LearningAgent,
Marketplace, Memory. Optionally publishes events.
"""
from __future__ import annotations

import datetime as _dt
from collections import Counter, defaultdict

from .goals import GoalEngine


class ExecutiveAgent:
    def __init__(self, collab_db, llm=None, finance_db_path=None):
        self.db = collab_db.conn
        self.llm = llm
        self.goals = GoalEngine(collab_db)
        self.finance_db_path = finance_db_path
        from ..collab.memory import MemoryManager
        from ..collab.learning import LearningAgent
        from ..collab.planner import PlannerAgent
        from ..product.marketplace import Marketplace
        self.memory = MemoryManager(collab_db)
        self.learning = LearningAgent(collab_db, self.memory)
        self.market = Marketplace(collab_db)
        self.planner = PlannerAgent(collab_db)

    # --- prioritize ---------------------------------------------------------
    def prioritize_goals(self) -> list[dict]:
        items = []
        for g in self.goals.overview():
            if g["status"] == "done":
                continue
            blocked = g["blocked"]
            prog = g["progress"] or 0
            score = (0 if blocked else 50) + prog * 0.3 + (10 if g["tasks"] else 0)
            items.append({
                "id": g["id"], "title": g["title"], "domain": g["domain"],
                "priority": round(score, 1), "blocked": blocked,
                "reason": "blocked by dependency" if blocked else ("in progress" if prog > 0 else "not started"),
            })
        items.sort(key=lambda x: -x["priority"])
        for i, it in enumerate(items):
            it["rank"] = i + 1
        return items

    # --- conflicts ----------------------------------------------------------
    def resolve_conflicts(self) -> list[dict]:
        conflicts = []
        goals = self.goals.overview()
        active = [g for g in goals if g["status"] == "active"]

        for g in active:
            if g["blocked"]:
                conflicts.append({"type": "blocked", "goal": g["title"],
                                  "detail": "waiting on: " + ", ".join(self._titles(g["depends_on"]))})

        dom = Counter(g["domain"] for g in active if not g["blocked"])
        for d, c in dom.items():
            if c > 1:
                conflicts.append({"type": "domain_contention", "domain": d,
                                  "detail": f"{c} active goals compete for '{d}' focus"})

        today = _dt.date.today().isoformat()
        for g in active:
            td = g.get("target_date")
            if td and str(td) < today:
                conflicts.append({"type": "overdue", "goal": g["title"], "detail": "past target " + str(td)})
        return conflicts

    # --- coordinate ---------------------------------------------------------
    def coordinate_agents(self) -> list[dict]:
        disabled = self.market.disabled_set()
        plan = []
        for g in self.prioritize_goals()[:5]:
            agent = f"{g['domain']}_agent"
            plan.append({"goal": g["title"], "domain": g["domain"], "agent": agent,
                         "enabled": agent not in disabled, "blocked": g["blocked"]})
        return plan

    # --- reprioritize domains ----------------------------------------------
    def reprioritize_domains(self, events=None) -> list[dict]:
        score = defaultdict(float)
        for g in self.prioritize_goals():
            score[g["domain"]] += g["priority"]
        for d, t in self.learning.trends().items():
            if t["trend"] == "increasing":
                score[d] += 20
            elif t["trend"] == "decreasing":
                score[d] -= 10
        order = sorted(score.items(), key=lambda kv: -kv[1])
        result = [{"domain": d, "score": round(s, 1)} for d, s in order]
        if events is not None:
            try:
                events.emit("domains.reprioritized", {"order": [d for d, _ in order]}, source="executive")
            except Exception:
                pass
        return result

    def brief(self, events=None) -> dict:
        """One-shot executive summary, optionally including finance drift."""
        result = {
            "priorities": self.prioritize_goals(),
            "conflicts": self.resolve_conflicts(),
            "coordination": self.coordinate_agents(),
            "domain_order": self.reprioritize_domains(events),
        }
        if self.finance_db_path:
            result["finance_drift"] = self.analyze_finance_drift()
        return result

    # ------------------------------------------------------------------
    # Finance goal drift analysis
    # ------------------------------------------------------------------

    _DRIFT_THRESHOLD = 0.30   # 30% — flag if required rate > actual by this margin

    def analyze_finance_drift(self) -> list[dict]:
        """
        For every active goal that has a finance_meta savings target set,
        compare the required monthly savings rate vs actual savings rate.

        Drift = (required - actual) / required
        Returns a list of drift reports; high_drift=True when drift > 30%.

        Savings rate is computed from the last 3 months of positive-amount
        transactions whose category matches the goal's monthly_savings_category.
        """
        import os
        import json
        if not self.finance_db_path or not os.path.exists(self.finance_db_path):
            return []

        reports = []
        try:
            from ..finance.engine import FinanceEngine
            fe = FinanceEngine(self.finance_db_path)
            try:
                reports = self._compute_drift(fe)
            finally:
                fe.close()
        except Exception:
            pass
        return reports

    def _compute_drift(self, finance_engine) -> list[dict]:
        import json
        today = _dt.date.today()
        reports = []

        active_goals = [g for g in self.goals.planner.list_goals()
                        if g.get("status") == "active"]

        for g in active_goals:
            target_data = self.planner.get_finance_target(g["id"])
            if not target_data:
                continue

            savings_target = float(target_data["savings_target"])
            category = target_data.get("monthly_savings_category", "Savings")

            # Months remaining until target_date
            target_date_str = g.get("target_date")
            if not target_date_str:
                continue
            try:
                target_date = _dt.date.fromisoformat(str(target_date_str)[:10])
            except ValueError:
                continue

            months_remaining = max(
                0.0,
                (target_date - today).days / 30.44
            )
            if months_remaining < 0.5:
                continue   # too close to matter

            required_monthly = savings_target / months_remaining

            # Actual monthly savings: avg of last 3 months, positive txns in category
            three_months_ago = (today - _dt.timedelta(days=91)).isoformat()
            txns = finance_engine.list_transactions(
                limit=2000, since=three_months_ago, category=category)
            savings_txns = [t["amount"] for t in txns if t["amount"] > 0]
            actual_monthly = (sum(savings_txns) / 3.0) if savings_txns else 0.0

            drift = ((required_monthly - actual_monthly) / required_monthly
                     if required_monthly > 0 else 0.0)
            high_drift = drift > self._DRIFT_THRESHOLD

            report = {
                "goal_id": g["id"],
                "goal_title": g["title"],
                "savings_target": savings_target,
                "category": category,
                "months_remaining": round(months_remaining, 1),
                "required_monthly": round(required_monthly, 2),
                "actual_monthly": round(actual_monthly, 2),
                "drift": round(drift, 3),
                "high_drift": high_drift,
                "target_date": target_date_str,
            }
            reports.append(report)
        return reports

    def _titles(self, ids) -> list[str]:
        out = []
        for i in ids:
            r = self.db.execute("SELECT title FROM goals WHERE id=?", (i,)).fetchone()
            if r:
                out.append(r["title"])
        return out
