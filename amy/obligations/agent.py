"""Obligation agent (Phase R7A-2) — turns obligation statuses into
notifications and payment PROPOSALS through the approval queue.

Runs as the daily `obligation_check` automation job. Kill switch:
AMY_AGENT_OBLIGATION (default ON — proposals only ever park in the queue,
so the destructive-default-off rule doesn't apply).
"""
from __future__ import annotations

import datetime as _dt

from .. import config
from . import all_statuses

_DUE_SOON_DAYS = 14


def obligation_check(ctx) -> dict:
    if not config.agent_enabled("obligation"):
        return {"skipped": "AMY_AGENT_OBLIGATION disabled"}
    from .. import tools

    fe = ctx.open_finance()
    try:
        statuses = all_statuses(fe)
    finally:
        fe.close()

    ns = ctx.notify_store()
    events = ctx.events()
    today = _dt.date.today().isoformat()
    notified = proposed = 0

    for st in statuses:
        state = st.get("state")
        due_amount = None
        if state == "accruing" and (st.get("days_to_due") or 99) <= _DUE_SOON_DAYS \
                and st.get("estimated_liability"):
            due_amount = st["estimated_liability"]
        elif state == "scheduled" and (st.get("days_to_due") or 99) <= _DUE_SOON_DAYS \
                and st.get("amount_due_by_next"):
            due_amount = st["amount_due_by_next"]
        elif state == "needs_estimate":
            ref = f"obl_estimate_{st['obligation_id']}"
            if not ns.exists_today("obligation_needs_estimate", ref):
                ns.create(type="obligation_needs_estimate",
                          title=f"Set an estimate for: {st['name']}",
                          body=(f"{st['name']} ({st['jurisdiction']}) needs an "
                                "estimated annual amount before installment "
                                f"figures can be computed. {st['disclaimer']}"),
                          priority="normal",
                          related_entity={"id": ref, "entity_type": "obligation",
                                          "obligation_id": st["obligation_id"]})
                notified += 1
            continue

        if due_amount is None:
            continue

        reasoning = (f"{st['name']} ({st['jurisdiction']} pack) is due "
                     f"{st.get('next_due')} — estimated "
                     f"{st['currency']} {due_amount:,.2f}. Rules used: "
                     f"rate={st['rules_shown'].get('rate')}, "
                     f"threshold={st['rules_shown'].get('wealth_threshold')}, "
                     f"calendar={st['rules_shown'].get('calendar_system')} "
                     f"(effective {st['rules_shown'].get('effective_from')}). "
                     "This is an estimate, not professional advice — approving "
                     "records the payment you make yourself; Amy never moves money.")

        ref = f"obl_due_{st['obligation_id']}_{st.get('next_due')}"
        if not ns.exists_today("obligation_due", ref):
            ns.create(type="obligation_due",
                      title=f"{st['name']} due {st.get('next_due')} "
                            f"(~{st['currency']} {due_amount:,.0f})",
                      body=reasoning, priority="high",
                      related_entity={"id": ref, "entity_type": "obligation",
                                      "obligation_id": st["obligation_id"]})
            notified += 1

        try:
            events.emit("agent.insight", {
                "agent": "obligation", "summary": f"{st['name']} due soon",
                "reasoning": reasoning, "obligation_id": st["obligation_id"],
            }, source="obligation_agent")
        except Exception:
            pass   # fire-and-forget (the notification above is the signal)

        # payment proposal — parks in the Approval Inbox (never auto-executes)
        ctx._extras["agent_name"] = "obligation_agent"
        ctx._extras["agent_reasoning"] = reasoning
        ctx._extras["agent_dedup_key"] = ref
        out = tools.invoke(ctx, "add_transaction", {
            "amount": -abs(float(due_amount)),
            "category": f"Obligation — {st['name']}",
            "merchant": st["name"],
            "notes": f"{st['jurisdiction']} pack estimate; {today}",
        }, actor="agent")
        if out.get("status") == "pending":
            proposed += 1

    return {"statuses": len(statuses), "notified": notified,
            "payment_proposals": proposed}
