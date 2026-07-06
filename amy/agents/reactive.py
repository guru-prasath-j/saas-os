"""Reactive agents — subscribers that act the moment data arrives (Phase R2).

Registered in the amy/events/triggers.py style onto whichever EventStore
instance is about to emit (both _emit_fin in the finance router and
JobCtx.events() in the automation layer call register_reactive_agents), so
reactions fire no matter which code path imported the data.

Agents:
  budget       — after an import OR a single manual transaction add,
                 re-checks caps vs actual spend (scoped to the affected
                 category on a manual add, so entering one transaction
                 doesn't re-notify about unrelated categories)
  subscription — after an import, proactively detects new recurring charges
                 and proposes them through the approval queue
  compliance   — after a ledger post, runs compliance suggestions for
                 'close'-tracked business entities (advisory rows only)

Rules honored here:
  - every insight/action carries an explicit reasoning string
  - kill switches: AMY_AGENT_BUDGET / _SUBSCRIPTION / _COMPLIANCE (default ON)
  - agent errors are reported as agent.error events, never raised into the
    emitting route (the bus additionally retries once + dead-letters)
  - all proposals go through the tool registry with actor="agent", so the
    R3 approval gate applies; nothing here writes user data directly
  - journaling via MemoryWriter.log_event is idempotent on event id
"""
from __future__ import annotations

from .. import config

_BUDGET_WARN_PCT = 0.90       # insight when a category crosses 90% of its cap
_SUB_MIN_CONFIDENCE = 0.75    # propose only confident subscription candidates
_IMPORT_EVENTS = ("finance.gmail_synced", "finance.csv_imported",
                  "finance.pdf_imported")
# Anything that adds transactions to the ledger — bulk imports plus a single
# manually-entered transaction (finance.transaction_added has no "imported"
# count; its presence alone means one row was just added).
_TRANSACTION_EVENTS = _IMPORT_EVENTS + ("finance.transaction_added",)


def _get_llm(ctx):
    """Lazy LLM: route-driven emissions build ctx without an LLM; agents that
    need one construct it once and cache it on the ctx."""
    if ctx.llm is not None:
        return ctx.llm
    cached = ctx._extras.get("lazy_llm")
    if cached is not None:
        return cached
    try:
        from ..llm import LLMRouter
        from ..automation.store import TrackedLLM
        llm = TrackedLLM(LLMRouter(use_global_keys=True), ctx.store,
                         purpose="reactive_agent")
    except Exception:
        llm = None
    ctx._extras["lazy_llm"] = llm
    return llm


def _journal(ctx, ev: dict) -> None:
    """Idempotent vault journaling of an agent event. Never raises."""
    try:
        from ..memory.writer import MemoryWriter
        from ..saas import paths
        vault = paths.vault_dir(ctx.user_id)
        vault.mkdir(parents=True, exist_ok=True)
        MemoryWriter(vault).log_event(ev)
    except Exception:
        pass   # journaling is best-effort; the event row is the record


def _emit_insight(events, ctx, agent: str, summary: str, reasoning: str,
                  extra: dict | None = None) -> None:
    payload = {"agent": agent, "summary": summary, "reasoning": reasoning}
    payload.update(extra or {})
    eid = events.emit("agent.insight", payload, source=f"{agent}_agent")
    _journal(ctx, {"id": eid, "type": "agent.insight", "payload": payload,
                   "ts": None, "source": f"{agent}_agent"})


def _report_error(events, agent: str, exc: Exception) -> None:
    try:
        events.emit("agent.error", {"agent": agent, "error": str(exc)[:400],
                                    "reasoning": "handler raised; see error"},
                    source=f"{agent}_agent")
    except Exception:
        pass   # the bus dead-letters the original failure regardless


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

def _budget_agent(events, ctx):
    def on_transaction_activity(ev):
        try:
            p = ev.get("payload") or {}
            is_manual_add = ev.get("type") == "finance.transaction_added"
            if not is_manual_add and not p.get("imported"):
                return   # bulk import brought in nothing new
            fe = ctx.open_finance()
            try:
                statuses = fe.budget_status()
            finally:
                fe.close()
            ns = ctx.notify_store()
            # A manual single add only needs to re-check the ONE category
            # that changed — otherwise entering one Food transaction would
            # re-notify about every other already-over-budget category too.
            # A bulk import may have touched several, so check them all.
            target_category = p.get("category") if is_manual_add else None
            for b in statuses:
                if not b.get("limit"):
                    continue
                if target_category and b["category"] != target_category:
                    continue
                spent, limit = b["spent"], b["limit"]
                if spent < limit * _BUDGET_WARN_PCT:
                    continue
                over = spent > limit
                state = "over budget" if over else f"at {spent / limit:.0%} of budget"
                trigger = ("You just added a transaction" if is_manual_add
                           else f"Import event {ev.get('id')} added transactions")
                reasoning = (f"{trigger}; '{b['category']}' now {state} "
                             f"(spent {spent:,.0f} of {limit:,.0f}).")
                summary = f"{b['category']} {state}"
                _emit_insight(events, ctx, "budget", summary, reasoning,
                              {"category": b["category"], "spent": spent,
                               "limit": limit, "source_event_id": ev.get("id")})
                ref = f"agent_budget_{b['category']}"
                if not ns.exists_today("agent_budget_check", ref):
                    ns.create(type="agent_budget_check",
                              title=f"Budget check: {summary}",
                              body=reasoning,
                              priority="high" if over else "normal",
                              related_entity={"id": ref, "entity_type": "budget",
                                              "category": b["category"]})
        except Exception as exc:
            _report_error(events, "budget", exc)

    for etype in _TRANSACTION_EVENTS:
        events.subscribe(etype, on_transaction_activity)


def _subscription_agent(events, ctx):
    def on_import(ev):
        try:
            if not (ev.get("payload") or {}).get("imported"):
                return
            from ..finance.subscription_detect import detect_subscriptions
            from .. import tools
            fe = ctx.open_finance()
            try:
                candidates = detect_subscriptions(fe, _get_llm(ctx))
            finally:
                fe.close()
            for c in candidates:
                if (c.get("confidence") or 0) < _SUB_MIN_CONFIDENCE:
                    continue
                reasoning = (f"Detected a recurring charge: '{c['name']}' "
                             f"~{c['amount']:,.0f}/{c.get('billing_cycle', 'monthly')}, "
                             f"seen {c.get('occurrences', '?')}x, last {c.get('last_date')}, "
                             f"confidence {c.get('confidence', 0):.0%}. Tracking it "
                             "enables renewal alerts and price-hike detection.")
                _emit_insight(events, ctx, "subscription",
                              f"New subscription detected: {c['name']}", reasoning,
                              {"name": c["name"], "amount": c["amount"],
                               "source_event_id": ev.get("id")})
                # propose through the registry → R3 gate parks it for approval
                ctx._extras["agent_name"] = "subscription_agent"
                ctx._extras["agent_reasoning"] = reasoning
                tools.invoke(ctx, "add_subscription",
                             {"name": c["name"], "monthly_cost": c["amount"],
                              "renewal_date": c.get("next_due")},
                             actor="agent")
        except Exception as exc:
            _report_error(events, "subscription", exc)

    for etype in _IMPORT_EVENTS:
        events.subscribe(etype, on_import)


def _screening_agent(events, ctx):
    """Values screening (R7A-1): after imports or manual adds, screen
    not-yet-checked transactions against the user's enabled ValuesProfiles.
    Flags carry reasoning and land in screening_flags (audit export);
    remediation (a review task) is proposed through the approval queue."""
    def on_new_transactions(ev):
        try:
            from ..values import (list_profiles, mark_screened, persist_flags,
                                  screen_transactions, unscreened_transactions)
            fe = ctx.open_finance()
            try:
                profiles = list_profiles(fe, enabled_only=True)
                if not profiles:
                    return
                txns = unscreened_transactions(fe, ctx.collab.conn)
                if not txns:
                    return
                flags = screen_transactions(fe, txns, profiles,
                                            llm=None)   # rules first; llm rules opt-in
            finally:
                fe.close()
            new = persist_flags(ctx.collab.conn, flags)
            mark_screened(ctx.collab.conn, [t["id"] for t in txns])
            if not new:
                return
            ns = ctx.notify_store()
            for f in flags[:5]:
                _emit_insight(events, ctx, "screening",
                              f"Values flag: {f['profile_name']}", f["reasoning"],
                              {"transaction_id": f["transaction_id"],
                               "severity": f["severity"],
                               "source_event_id": ev.get("id")})
            ref = f"screening_{ev.get('id')}"
            if not ns.exists_today("values_flag", ref):
                ns.create(type="values_flag",
                          title=f"{new} transaction(s) flagged by your values profiles",
                          body="; ".join(f["reasoning"] for f in flags[:3]),
                          priority="high" if any(f["severity"] == "high"
                                                 for f in flags) else "normal",
                          related_entity={"id": ref, "entity_type": "screening"})
            # remediation via the queue: a concrete review task, never a
            # silent data change
            from .. import tools
            from ..autonomous import GoalEngine
            goals = GoalEngine(ctx.collab)
            row = ctx.collab.conn.execute(
                "SELECT id FROM goals WHERE domain='finance'"
                " AND title='Values Review' AND status='active' LIMIT 1").fetchone()
            goal_id = row["id"] if row else goals.create_goal(
                "Values Review", domain="finance")
            worst = max(flags, key=lambda f: f["severity"] == "high")
            ctx._extras["agent_name"] = "screening_agent"
            ctx._extras["agent_reasoning"] = worst["reasoning"]
            ctx._extras["agent_dedup_key"] = f"values_task_{worst['transaction_id']}"
            tools.invoke(ctx, "add_goal_task",
                         {"goal_id": goal_id,
                          "title": f"Review flagged transaction: {worst['reasoning'][:120]}"},
                         actor="agent")
        except Exception as exc:
            _report_error(events, "screening", exc)

    for etype in _TRANSACTION_EVENTS:
        events.subscribe(etype, on_new_transactions)


def _compliance_agent(events, ctx):
    def on_ledger_posted(ev):
        try:
            p = ev.get("payload") or {}
            entity_id = p.get("entity_id") or p.get("business_entity_id")
            if not entity_id:
                return
            fe = ctx.open_finance()
            try:
                entity = fe.get_business_entity(entity_id)
                if entity is None:
                    return
                if entity.get("tracking_closeness") != "close":
                    # loose tracking: the user asked for a lighter touch —
                    # do not run anything automatically (same gate the
                    # Auditor honors)
                    return
                pending = fe.ledger_entries_without_suggestions(entity_id)
                if not pending:
                    return
                from ..finance.business.compliance import generate_suggestions
                suggestions = generate_suggestions(fe, entity, _get_llm(ctx))
            finally:
                fe.close()
            reasoning = (f"Ledger entry posted for '{entity.get('name')}' "
                         f"(event {ev.get('id')}); entity is tracked 'close' and had "
                         f"{len(pending)} entr(y/ies) without compliance review — "
                         f"generated {len(suggestions)} advisory suggestion(s). "
                         "Suggestions are estimates, not professional tax advice.")
            _emit_insight(events, ctx, "compliance",
                          f"Compliance review: {entity.get('name')}", reasoning,
                          {"entity_id": entity_id,
                           "suggestions": len(suggestions),
                           "source_event_id": ev.get("id")})
        except Exception as exc:
            _report_error(events, "compliance", exc)

    events.subscribe("finance.ledger_entry_posted", on_ledger_posted)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_reactive_agents(events, ctx) -> list[str]:
    """Wire enabled reactive agents onto this EventStore instance.
    Returns the list of agents registered (for tests/observability)."""
    registered = []
    if config.agent_enabled("budget"):
        _budget_agent(events, ctx)
        registered.append("budget")
    if config.agent_enabled("subscription"):
        _subscription_agent(events, ctx)
        registered.append("subscription")
    if config.agent_enabled("compliance"):
        _compliance_agent(events, ctx)
        registered.append("compliance")
    if config.agent_enabled("screening"):
        _screening_agent(events, ctx)
        registered.append("screening")
    return registered
