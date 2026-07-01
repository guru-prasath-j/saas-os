"""Digest generation for the scheduler.

`generate_and_store(collab_db)` builds the proactive digest for one user and
records a `digest.generated` event (which triggers can react to and the
/api/digest/latest endpoint can read). The SaaS app's background loop calls this
for every active user on an interval.

Imports are lazy to avoid an import cycle (events <- collab <- events).
"""
from __future__ import annotations

import datetime as _dt


def generate_and_store(collab_db, days: int = 7,
                       finance_db_path: str | None = None,
                       user_email: str | None = None,
                       llm=None) -> dict:
    from ..collab import MemoryManager, PlannerAgent, ReflectionAgent, LearningAgent
    from ..product import build_suggestions
    from .triggers import build_digest
    from .store import EventStore

    mem = MemoryManager(collab_db)
    planner = PlannerAgent(collab_db)
    reflection = ReflectionAgent(collab_db, planner, mem)
    learning = LearningAgent(collab_db, mem)

    digest = build_digest(reflection, learning, planner, build_suggestions, days)

    # Optional auto-categorization pass (runs before digest so summary is accurate)
    categorization_result: dict = {}
    if finance_db_path and llm is not None:
        import os
        if os.path.exists(finance_db_path):
            try:
                from ..finance import FinanceEngine
                from ..finance.categorizer import FinanceCategorizer
                fe_cat = FinanceEngine(finance_db_path)
                try:
                    categorization_result = FinanceCategorizer().auto_categorize(fe_cat, llm)
                finally:
                    fe_cat.close()
            except Exception:
                pass

    # Optional finance digest pass
    finance_summary: dict = {}
    if finance_db_path:
        import os
        if os.path.exists(finance_db_path):
            try:
                from ..finance import FinanceEngine
                fe = FinanceEngine(finance_db_path)
                try:
                    ov = fe.overview()
                    over = [b["category"] for b in ov["budget_status"] if b["over_budget"]]
                    finance_summary = {
                        "balance_estimate": ov["balance_estimate"],
                        "subscription_monthly_total": ov["subscription_monthly_total"],
                        "over_budget_categories": over,
                        "upcoming_bills": [
                            {"name": b["name"], "date": b["renewal_date"],
                             "cost": b["monthly_cost"]}
                            for b in ov["upcoming_bills"][:5]
                        ],
                    }
                finally:
                    fe.close()
            except Exception:
                pass

    # Cash-flow forecast — compute before payload so it's included in the emitted event
    cashflow_forecast: dict = {}
    if finance_db_path:
        import os
        if os.path.exists(finance_db_path):
            try:
                from ..finance import FinanceEngine
                from ..engines.predictive_engine import PredictiveEngine
                fe_cf = FinanceEngine(finance_db_path)
                try:
                    cashflow_forecast = PredictiveEngine(None).forecast_finance(fe_cf)
                finally:
                    fe_cf.close()
            except Exception:
                pass

    payload: dict = {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "suggestion_count": len(digest["suggestions"]),
        "suggestions": digest["suggestions"],
        "open_goals": [g["title"] for g in digest["open_goals"]],
        "recommendations": digest["recommendations"],
    }
    if finance_summary:
        payload["finance"] = finance_summary
    if categorization_result:
        payload["auto_categorization"] = categorization_result
    if cashflow_forecast:
        payload["cashflow_forecast"] = cashflow_forecast

    EventStore(collab_db).emit("digest.generated", payload, source="scheduler")

    # Create in-app notifications + optional email alerts from finance conditions
    if finance_summary:
        try:
            from ..notifications import NotificationStore, NotificationService
            from ..notifications.email import maybe_send_alert
            store = NotificationStore(collab_db)
            svc = NotificationService(store)
            if finance_db_path:
                import os
                if os.path.exists(finance_db_path):
                    from ..finance import FinanceEngine
                    fe = FinanceEngine(finance_db_path)
                    try:
                        created_ids = svc.evaluate_finance(fe)
                        # Cash-flow alert notification (high priority)
                        if cashflow_forecast.get("alert"):
                            ref_id = "cashflow_alert_weekly"
                            if not store.exists_today("cashflow_alert", ref_id):
                                cf = cashflow_forecast
                                nid = store.create(
                                    type="cashflow_alert",
                                    title="Cash-flow alert: spending pace too high",
                                    body=cf.get("note", ""),
                                    priority="high",
                                    related_entity={"id": ref_id,
                                                    "entity_type": "cashflow",
                                                    "projected": cf.get("projected_next_week_spend")},
                                )
                                created_ids.append(nid)
                        if user_email and created_ids:
                            for nid in created_ids:
                                notifs = [n for n in store.list() if n["id"] == nid]
                                if notifs:
                                    maybe_send_alert(user_email, notifs[0])
                    finally:
                        fe.close()
        except Exception:
            pass

    # Goal drift analysis — emit high-priority notifications when drift > 30%
    if finance_db_path:
        try:
            from ..notifications import NotificationStore
            from ..autonomous.executive import ExecutiveAgent
            exec_agent = ExecutiveAgent(collab_db, finance_db_path=finance_db_path)
            drift_reports = exec_agent.analyze_finance_drift()
            drift_store = NotificationStore(collab_db)
            for report in drift_reports:
                if not report.get("high_drift"):
                    continue
                ref_id = f"drift_{report['goal_id']}"
                if drift_store.exists_today("goal_drift", ref_id):
                    continue
                drift_store.create(
                    type="goal_drift",
                    title=f"Savings goal at risk: {report['goal_title']}",
                    body=(
                        f"You need ₹{report['required_monthly']:,.0f}/month but are "
                        f"saving ₹{report['actual_monthly']:,.0f}/month — "
                        f"{int(report['drift']*100)}% behind. "
                        f"{report['months_remaining']:.1f} months to target date."
                    ),
                    priority="high",
                    related_entity={"id": ref_id, "entity_type": "goal",
                                    "goal_id": report["goal_id"]},
                )
        except Exception:
            pass

    return {**digest, **({"finance": finance_summary} if finance_summary else {})}
