"""Daily sentinels (Phase 3) — anomaly detection + goal cashflow drift.

Pure tier-1 behaviour: nothing is changed, the user is alerted through the
existing NotificationStore (deduped per day via exists_today) only when
something is off. Silence means healthy.
"""
from __future__ import annotations

import calendar
import datetime as _dt
from collections import defaultdict

from .executors import JobCtx

_LARGE_DEBIT_FLOOR = 5000.0       # never flag below this (₹)
_LARGE_DEBIT_MULTIPLIER = 3.0     # × trailing-90d mean absolute debit
_PRICE_HIKE_TOLERANCE = 1.10      # 10% above tracked monthly cost
_RUNRATE_GRACE_DAYS = 7           # projections are noise in the first week


def anomaly_sentinel(ctx: JobCtx) -> dict:
    fe = ctx.open_finance()
    ns = ctx.notify_store()
    created = []
    try:
        today = _dt.date.today()
        since_90 = (today - _dt.timedelta(days=90)).isoformat()
        since_7 = (today - _dt.timedelta(days=7)).isoformat()
        txns = fe.list_transactions(limit=2000, since=since_90)
        debits = [t for t in txns if (t["amount"] or 0) < 0]

        # 1 — possible double charges: same day + merchant + amount, last 7 days
        buckets: dict[tuple, int] = defaultdict(int)
        for t in debits:
            if t["date"] >= since_7 and (t["merchant"] or "").strip():
                buckets[(t["date"], t["merchant"], t["amount"])] += 1
        for (date, merchant, amount), n in buckets.items():
            if n < 2:
                continue
            ref = f"dup_{date}_{merchant[:30]}_{amount}"
            if ns.exists_today("possible_double_charge", ref):
                continue
            created.append(ns.create(
                type="possible_double_charge",
                title=f"Possible double charge: {merchant[:60]}",
                body=(f"{n} identical debits of ₹{abs(amount):,.0f} to "
                      f"'{merchant}' on {date}. Check whether one should be reversed."),
                priority="high",
                related_entity={"entity_type": "anomaly", "id": ref}))

        # 2 — unusually large debit in the last 3 days
        if debits:
            mean_abs = sum(abs(t["amount"]) for t in debits) / len(debits)
            threshold = max(_LARGE_DEBIT_FLOOR, _LARGE_DEBIT_MULTIPLIER * mean_abs)
            since_3 = (today - _dt.timedelta(days=3)).isoformat()
            for t in debits:
                if t["date"] < since_3 or abs(t["amount"]) < threshold:
                    continue
                if t.get("category") in ("Transfer", "Investment", "Custodial Disbursement"):
                    continue   # deliberate money movement, not spend
                ref = f"large_{t['id']}"
                if ns.exists_today("large_debit", ref):
                    continue
                created.append(ns.create(
                    type="large_debit",
                    title=f"Large debit: ₹{abs(t['amount']):,.0f}",
                    body=(f"₹{abs(t['amount']):,.0f} to '{t['merchant']}' on {t['date']} "
                          f"— {_LARGE_DEBIT_MULTIPLIER:.0f}× above your typical debit "
                          f"(₹{mean_abs:,.0f})."),
                    priority="high",
                    related_entity={"entity_type": "transaction", "id": ref,
                                    "transaction_id": t["id"]}))

        # 3 — subscription price hikes
        for sub in fe.list_subscriptions(status="active"):
            cost = sub.get("monthly_cost") or 0
            if cost <= 0:
                continue
            tokens = [w for w in sub["name"].lower().split() if len(w) >= 4]
            if not tokens:
                continue
            recent = [t for t in debits if t["date"] >= since_7
                      and any(tok in (t["merchant"] or "").lower() for tok in tokens)]
            for t in recent:
                if abs(t["amount"]) <= cost * _PRICE_HIKE_TOLERANCE:
                    continue
                ref = f"hike_{sub['id']}"
                if ns.exists_today("subscription_price_hike", ref):
                    continue
                created.append(ns.create(
                    type="subscription_price_hike",
                    title=f"Price hike: {sub['name']}",
                    body=(f"Latest charge ₹{abs(t['amount']):,.0f} vs tracked "
                          f"₹{cost:,.0f}/mo (+{(abs(t['amount']) / cost - 1) * 100:.0f}%). "
                          "Update the subscription or review the plan."),
                    priority="normal",
                    related_entity={"entity_type": "subscription", "id": ref,
                                    "subscription_id": sub["id"]}))
                break

        # 4 — budget run-rate breach BEFORE month-end
        if today.day >= _RUNRATE_GRACE_DAYS:
            days_in_month = calendar.monthrange(today.year, today.month)[1]
            for b in fe.budget_status():
                if b["over_budget"]:
                    continue   # actual overage handled by NotificationService
                projected = b["spent"] / today.day * days_in_month
                if projected <= b["limit"] * 1.05:
                    continue
                ref = f"runrate_{b['category']}"
                if ns.exists_today("budget_runrate", ref):
                    continue
                created.append(ns.create(
                    type="budget_runrate",
                    title=f"On track to exceed budget: {b['category']}",
                    body=(f"₹{b['spent']:,.0f} spent so far → projected "
                          f"₹{projected:,.0f} by month-end against a "
                          f"₹{b['limit']:,.0f} limit."),
                    priority="normal",
                    related_entity={"entity_type": "budget", "id": ref,
                                    "category": b["category"]}))
    finally:
        fe.close()
    return {"alerts_created": len(created)}


def cashflow_alerts(ctx: JobCtx) -> dict:
    """Savings-goal drift (Executive's analysis, finally consumed automatically)."""
    from ..autonomous import ExecutiveAgent

    ns = ctx.notify_store()
    created = []
    ex = ExecutiveAgent(ctx.collab, llm=None, finance_db_path=ctx.finance_path)
    for report in ex.analyze_finance_drift():
        if not report.get("high_drift"):
            continue
        ref = f"drift_{report['goal_id']}"
        if ns.exists_today("goal_cashflow_drift", ref):
            continue
        created.append(ns.create(
            type="goal_cashflow_drift",
            title=f"Savings drift: {report['goal_title']}",
            body=(f"You need ₹{report['required_monthly']:,.0f}/mo to hit "
                  f"'{report['goal_title']}' by {report['target_date']} but are "
                  f"saving ₹{report['actual_monthly']:,.0f}/mo "
                  f"({report['drift'] * 100:.0f}% behind)."),
            priority="high",
            related_entity={"entity_type": "goal", "id": ref,
                            "goal_id": report["goal_id"]}))
    return {"drift_reports": len(created)}
