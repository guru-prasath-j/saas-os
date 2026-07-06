"""Bigger scheduled workflows: monthly close, custodial autopilot,
morning briefing, and the daily Autopilot run (Phases 3–4).
"""
from __future__ import annotations

import calendar
import datetime as _dt

from .executors import JobCtx, submit_action
from . import learning

_SUB_SUGGESTION_MIN_CONFIDENCE = 0.7


# ---------------------------------------------------------------------------
# Monthly close — runs on the 1st, reports on the month that just ended
# ---------------------------------------------------------------------------

def monthly_close(ctx: JobCtx) -> dict:
    from ..finance.categorizer import auto_categorize_all
    from ..finance.subscription_detect import detect_subscriptions

    today = _dt.date.today()
    first_this = today.replace(day=1)
    last_month_end = first_this - _dt.timedelta(days=1)
    month_start = last_month_end.replace(day=1)
    month_label = month_start.strftime("%B %Y")

    fe = ctx.open_finance()
    report_lines: list[str] = []
    result: dict = {"month": month_label}
    try:
        # 1 — clean categorisation first so the numbers below are honest
        learned = learning.apply_learned_rules(fe)
        recategorized = auto_categorize_all(fe, llm=ctx.llm)
        result["recategorized"] = learned + recategorized

        # 2 — budget vs actual for the closed month
        spent_by_cat: dict[str, float] = {}
        for t in fe.list_transactions(limit=5000,
                                      since=month_start.isoformat(),
                                      until=last_month_end.isoformat()):
            if (t["amount"] or 0) < 0:
                spent_by_cat[t["category"]] = (
                    spent_by_cat.get(t["category"], 0.0) + abs(t["amount"]))
        budgets = {b["category"]: b["monthly_limit"] for b in fe.list_budgets()}
        overages = []
        for cat, limit in budgets.items():
            spent = spent_by_cat.get(cat, 0.0)
            if spent > limit:
                overages.append(f"{cat}: ₹{spent:,.0f} / ₹{limit:,.0f}")
        total_spend = sum(spent_by_cat.values())
        report_lines.append(f"Total spend in {month_label}: ₹{total_spend:,.0f}.")
        report_lines.append(
            ("Over budget — " + "; ".join(overages)) if overages
            else "All budgets held.")
        result["total_spend"] = round(total_spend, 2)
        result["overages"] = len(overages)

        # 3 — newly detected subscriptions → Approval Inbox (tier 2)
        proposed_subs = 0
        try:
            for s in detect_subscriptions(fe, ctx.llm):
                if (s.get("confidence") or 0) < _SUB_SUGGESTION_MIN_CONFIDENCE:
                    continue
                r = submit_action(
                    ctx, tier=2, action_type="add_subscription",
                    title=f"Track new subscription: {s['name']}",
                    body=(f"₹{s['amount']:,.0f}/{s.get('billing_cycle', 'monthly')} "
                          f"seen {s.get('occurrences', '?')}× (last {s.get('last_date')}). "
                          f"Confidence {s.get('confidence', 0):.0%}."),
                    payload={"name": s["name"], "monthly_cost": s["amount"],
                             "renewal_date": s.get("next_due")},
                    source="monthly_close",
                    dedup_key=f"sub_{s['name'].lower()}_{today.strftime('%Y-%m')}")
                if r["status"] == "pending":
                    proposed_subs += 1
        except Exception:
            pass
        if proposed_subs:
            report_lines.append(
                f"{proposed_subs} new subscription(s) detected — awaiting approval.")
        result["subscriptions_proposed"] = proposed_subs

        # 4 — business entities: refresh compliance suggestions
        compliance_new = 0
        try:
            from ..finance.business.compliance import generate_suggestions
            for entity in fe.list_business_entities():
                try:
                    compliance_new += len(generate_suggestions(fe, entity, ctx.llm))
                except Exception:
                    pass
        except Exception:
            pass
        if compliance_new:
            report_lines.append(
                f"{compliance_new} new compliance suggestion(s) across business entities.")
        result["compliance_suggestions"] = compliance_new
    finally:
        fe.close()

    # 5 — publish the CFO report
    body = " ".join(report_lines)
    try:
        ns = ctx.notify_store()
        ref = f"close_{month_start.strftime('%Y-%m')}"
        if not ns.exists_today("monthly_close", ref):
            ns.create(type="monthly_close",
                      title=f"Monthly CFO report — {month_label}",
                      body=body, priority="normal",
                      related_entity={"entity_type": "report", "id": ref})
    except Exception:
        pass
    try:
        from ..collab.memory import MemoryManager
        MemoryManager(ctx.collab).add_summary(
            f"Monthly close ({month_label}): {body}")
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Custodial autopilot — proposal is prepared; the user only taps approve
# ---------------------------------------------------------------------------

def custodial_autopilot(ctx: JobCtx) -> dict:
    from ..finance.custodial import next_cycle_prefill, run_validation

    today = _dt.date.today().isoformat()
    fe = ctx.open_finance()
    proposed = 0
    try:
        for acc in fe.list_accounts():
            if acc.get("account_type") != "custodial":
                continue
            prefill = next_cycle_prefill(fe, acc["id"])
            due = prefill.get("due_date")
            if not due or due > today:
                continue
            disbursements = [
                {"beneficiary_id": b["beneficiary_id"], "name": b["name"],
                 "amount": b["last_amount"]}
                for b in prefill["beneficiaries"] if b.get("last_amount")
            ]
            if not disbursements:
                continue
            total = sum(d["amount"] for d in disbursements)
            issues = run_validation(fe, acc["id"]).get("issues", [])
            warn = f" ⚠ {len(issues)} validation issue(s)." if issues else ""
            names = ", ".join(f"{d['name']} ₹{d['amount']:,.0f}" for d in disbursements)
            r = submit_action(
                ctx, tier=2, action_type="custodial_disburse",
                title=f"Custodial cycle due ({acc.get('nickname') or acc.get('bank_name')})",
                body=(f"Due {due}. Prefilled from last cycle: {names} "
                      f"(total ₹{total:,.0f}).{warn} Approve after sending the "
                      "transfers — this records them and updates the Sheet."),
                payload={"account_id": acc["id"], "disbursements": disbursements},
                source="custodial_autopilot",
                dedup_key=f"custodial_{acc['id']}_{due}")
            if r["status"] == "pending":
                proposed += 1
    finally:
        fe.close()
    return {"cycles_proposed": proposed}


# ---------------------------------------------------------------------------
# Morning briefing — one daily message that closes the loop
# ---------------------------------------------------------------------------

def morning_briefing(ctx: JobCtx) -> dict:
    from ..notifications.email import send_email, smtp_configured

    today = _dt.date.today().isoformat()
    lines: list[str] = []

    fe = ctx.open_finance()
    try:
        spent = sum(fe.this_month_spend().values())
        lines.append(f"Balance est. ₹{fe.balance_estimate():,.0f}; "
                     f"this month's spend ₹{spent:,.0f}.")
    except Exception:
        pass
    finally:
        fe.close()

    pending = ctx.store.list_approvals("pending", limit=5)
    if pending:
        lines.append(f"{ctx.store.pending_count()} approval(s) waiting: "
                     + "; ".join(p["title"] for p in pending) + ".")
    else:
        lines.append("No approvals waiting.")

    try:
        from ..autonomous import ExecutiveAgent
        brief = ExecutiveAgent(ctx.collab, llm=None,
                               finance_db_path=ctx.finance_path)
        prios = brief.prioritize_goals()[:3]
        if prios:
            lines.append("Top goals: "
                         + "; ".join(p["title"] for p in prios) + ".")
        conflicts = brief.resolve_conflicts()
        if conflicts:
            lines.append(f"{len(conflicts)} goal conflict(s) need attention.")
    except Exception:
        pass

    ns = ctx.notify_store()
    try:
        unread = ns.unread_count()
        if unread:
            lines.append(f"{unread} unread notification(s).")
    except Exception:
        pass

    body = " ".join(lines)
    ref = f"briefing_{today}"
    created = None
    if not ns.exists_today("morning_briefing", ref):
        created = ns.create(type="morning_briefing",
                            title=f"Morning briefing — {today}",
                            body=body, priority="normal",
                            related_entity={"entity_type": "briefing", "id": ref})
        if smtp_configured() and ctx.user_email:
            send_email(ctx.user_email, f"[Amy] Morning briefing — {today}", body)
    return {"created": bool(created), "summary": body}


# ---------------------------------------------------------------------------
# Daily Autopilot — the existing additive-only engine, finally on a schedule
# ---------------------------------------------------------------------------

def autopilot_run(ctx: JobCtx) -> dict:
    from ..autonomous import Autopilot

    ap = Autopilot(ctx.collab, llm=ctx.llm, events=ctx.events(),
                   finance_db_path=ctx.finance_path)
    out = ap.run(dry_run=False)
    return {"actions": out.get("count", 0)}
