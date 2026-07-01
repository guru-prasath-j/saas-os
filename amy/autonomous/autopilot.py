"""Autopilot (PIOS v3) — closes the action loop.

The Executive Agent recommends; Autopilot *acts* on those recommendations, within
strict safety rails:
  - ONLY additive / reversible actions (enable an agent, add planning tasks,
    flag a conflict as an event). It NEVER disables agents, deletes anything,
    or takes financial/irreversible actions.
  - dry_run=True previews actions without applying them.
  - a per-run cap (max_actions) bounds how much it does.
  - every action is logged as an `action.taken` / `autopilot.run` event + memory note.

What it does each run:
  1. Enable the domain agents the top active goals need (if disabled).
  2. Advance stalled active+unblocked goals by adding starter tasks (LLM if available, else heuristic).
  3. Flag overdue / blocked goals as `conflict.flagged` events.
"""
from __future__ import annotations

from .executive import ExecutiveAgent


class Autopilot:
    def __init__(self, collab_db, llm=None, events=None, finance_db_path=None):
        self.db = collab_db.conn
        self.llm = llm
        self.exec = ExecutiveAgent(collab_db, llm)
        self.goals = self.exec.goals
        self.market = self.exec.market
        self.memory = self.exec.memory
        self.finance_db_path = finance_db_path
        from ..events import EventStore
        self.events = events or EventStore(collab_db)

    def _starter_steps(self, title: str) -> list[str]:
        if self.llm is not None:
            try:
                txt, _ = self.llm.generate(
                    "Break this goal into 3 concrete first tasks. One per line, no numbering.",
                    title, "")
                steps = [s.strip(" -•\t") for s in txt.split("\n") if s.strip()]
                if steps:
                    return steps[:3]
            except Exception:
                pass
        return [f"Define what 'done' means for: {title}",
                f"Do the first concrete step for: {title}",
                f"Block time this week for: {title}"]

    def run(self, dry_run: bool = False, max_actions: int = 12) -> dict:
        actions = []
        priorities = self.exec.prioritize_goals()
        overview = self.goals.overview()
        disabled = self.market.disabled_set()

        # 1) enable agents the top active goals need
        for p in priorities[:5]:
            if p["blocked"]:
                continue
            agent = f"{p['domain']}_agent"
            if agent in disabled:
                if not dry_run:
                    self.market.enable(agent)
                    self.events.emit("action.taken", {"action": "enable_agent", "target": agent}, source="autopilot")
                actions.append({"action": "enable_agent", "target": agent,
                                "why": f"needed for goal '{p['title']}'"})

        # 2) advance stalled active+unblocked goals (no milestones, no tasks)
        for g in overview:
            if len(actions) >= max_actions:
                break
            if g["status"] != "active" or g["blocked"]:
                continue
            if not g["tasks"] and not g.get("milestones"):
                steps = self._starter_steps(g["title"])[:3]
                if not dry_run:
                    for s in steps:
                        self.goals.add_task(g["id"], s)
                    self.events.emit("action.taken",
                                     {"action": "advance_goal", "target": g["title"], "tasks": steps},
                                     source="autopilot")
                actions.append({"action": "advance_goal", "target": g["title"], "added_tasks": steps})

        # 3) flag conflicts (overdue / blocked) as events
        conflicts = self.exec.resolve_conflicts()
        for c in conflicts:
            if len(actions) >= max_actions:
                break
            if c["type"] in ("overdue", "blocked"):
                if not dry_run:
                    self.events.emit("conflict.flagged", c, source="autopilot")
                actions.append({"action": "flag_conflict", "detail": c})

        # 4) flag unused subscriptions as review tasks
        if self.finance_db_path and len(actions) < max_actions:
            fin_actions = self._finance_actions(dry_run=dry_run)
            actions.extend(fin_actions[:max(0, max_actions - len(actions))])

        if not dry_run:
            self.events.emit("autopilot.run", {"actions": len(actions)}, source="autopilot")
            self.memory.add_summary(f"Autopilot ran — {len(actions)} action(s) taken")

        return {
            "dry_run": dry_run,
            "count": len(actions),
            "actions": actions[:max_actions],
            "priorities": priorities,
            "conflicts": conflicts,
        }

    # ------------------------------------------------------------------
    # Finance: unused subscription detection
    # ------------------------------------------------------------------

    _UNUSED_LOOKBACK_DAYS = 60
    _MAX_UNUSED_FLAGS = 3

    def _finance_actions(self, dry_run: bool = False) -> list[dict]:
        """
        Cross-references active subscriptions against recent transactions.
        If a subscription name has no matching merchant in the last 60 days,
        create a GoalEngine review task (domain='finance') and emit action.taken.

        Matching: any token ≥4 chars from the subscription name found anywhere
        in the transaction's merchant or notes field (case-insensitive).
        """
        import os
        if not os.path.exists(self.finance_db_path):
            return []
        actions: list[dict] = []
        try:
            from ..finance.engine import FinanceEngine
            import datetime as _dt
            fe = FinanceEngine(self.finance_db_path)
            try:
                since = (_dt.date.today()
                         - _dt.timedelta(days=self._UNUSED_LOOKBACK_DAYS)).isoformat()
                txns = fe.list_transactions(limit=2000, since=since)
                # Build a flat search corpus per transaction
                corpus = [
                    (t["merchant"] or "").lower() + " " + (t["notes"] or "").lower()
                    for t in txns
                ]

                for sub in fe.list_subscriptions(status="active"):
                    if len(actions) >= self._MAX_UNUSED_FLAGS:
                        break
                    if not sub.get("monthly_cost", 0):
                        continue  # free tier — not worth flagging
                    tokens = [
                        tok for tok in sub["name"].lower().split() if len(tok) >= 4
                    ]
                    if not tokens:
                        continue
                    matched = any(
                        any(tok in entry for tok in tokens)
                        for entry in corpus
                    )
                    if matched:
                        continue  # subscription appears to be used

                    task_title = (
                        f"Review '{sub['name']}' subscription "
                        f"— no transactions found in last {self._UNUSED_LOOKBACK_DAYS} days "
                        f"(₹{sub['monthly_cost']:,.0f}/mo)"
                    )
                    if not dry_run:
                        # Find or create a finance review goal
                        goal_id = self._ensure_finance_review_goal()
                        self.goals.add_task(goal_id, task_title)
                        self.events.emit(
                            "action.taken",
                            {"action": "flag_unused_subscription",
                             "target": sub["name"],
                             "monthly_cost": sub["monthly_cost"]},
                            source="autopilot",
                        )
                    actions.append({
                        "action": "flag_unused_subscription",
                        "target": sub["name"],
                        "monthly_cost": sub["monthly_cost"],
                        "why": f"no transactions matching '{sub['name']}' in last "
                               f"{self._UNUSED_LOOKBACK_DAYS} days",
                    })
            finally:
                fe.close()
        except Exception:
            pass
        return actions

    def _ensure_finance_review_goal(self) -> str:
        """Return existing 'Finance Review' goal id or create one."""
        existing = self.db.execute(
            "SELECT id FROM goals WHERE domain='finance' AND title='Finance Review'"
            " AND status='active' LIMIT 1"
        ).fetchone()
        if existing:
            return existing["id"]
        return self.goals.create_goal("Finance Review", domain="finance")
