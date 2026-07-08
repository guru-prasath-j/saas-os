"""Built-in tools — wrap existing engines behind the registry.

Import this module once (amy/tools/__init__.py does) and every tool is
registered. Write/destructive tools delegate to the automation executor
registry (amy/automation/executors.py) whenever an executor exists, so the
approval queue executes approved actions through the exact same code path.

No new business logic lives here: handlers call FinanceEngine, afford,
business modules, MemoryWriter, GraphStore, GoalEngine, EventStore.
"""
from __future__ import annotations

import datetime as _dt
import uuid
from pathlib import Path

from .registry import (RISK_DESTRUCTIVE, RISK_READ, RISK_WRITE, ToolError,
                       register_tool)


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


def _index_dir(ctx) -> Path:
    return Path(ctx.finance_path).parent


_CUSTODIAL_CATEGORY_THRESHOLD = 0.9


def is_custodial_category(fe, category: str) -> bool:
    """True if >=90% of this category's transaction volume (by absolute
    amount) originates from custodial accounts — i.e. money forwarded to
    beneficiaries, not the user's own discretionary spending.

    Read-only signal for tool/agent layers (surfaced in list_budgets and
    used to warn on set_budget proposals); never touches custodial.py's
    disbursement/refill logic itself. Found via manual testing: the
    orchestrator proposed cutting a 'Custodial Disbursement' budget as if
    it were personal spending — this flag lets both the LLM and the human
    reviewer catch that."""
    rows = fe.conn.execute(
        "SELECT t.amount, a.account_type FROM transactions t"
        " LEFT JOIN accounts a ON t.account_id = a.id WHERE t.category=?",
        (category,)).fetchall()
    total = sum(abs(r["amount"] or 0) for r in rows)
    if total <= 0:
        return False
    custodial_total = sum(abs(r["amount"] or 0) for r in rows
                          if (r["account_type"] or "") == "custodial")
    return (custodial_total / total) >= _CUSTODIAL_CATEGORY_THRESHOLD


# ===========================================================================
# READ tools
# ===========================================================================

@register_tool("finance_overview",
               "Snapshot: monthly income, this month's spend by category, "
               "balance estimate, budget status, upcoming bills, portfolio.",
               _obj({}), RISK_READ)
def _t_overview(ctx, args):
    fe = ctx.open_finance()
    try:
        return fe.overview()
    finally:
        fe.close()


@register_tool("list_transactions",
               "Recent transactions, newest first. All filters optional.",
               _obj({"limit": {"type": "integer", "description": "max rows (<=100)"},
                     "category": {"type": "string"},
                     "since": {"type": "string", "description": "YYYY-MM-DD"},
                     "until": {"type": "string", "description": "YYYY-MM-DD"},
                     "account_id": {"type": "string"}}), RISK_READ)
def _t_list_txns(ctx, args):
    fe = ctx.open_finance()
    try:
        return fe.list_transactions(
            limit=min(int(args.get("limit") or 20), 100),
            category=args.get("category"), since=args.get("since"),
            until=args.get("until"), account_id=args.get("account_id"))
    finally:
        fe.close()


@register_tool("list_budgets",
               "Budget caps with current-month spend/headroom. A row with "
               "custodial_category:true means that category's money is "
               "pass-through funds forwarded to beneficiaries from a "
               "custodial account — NOT the user's own discretionary "
               "spending. Never propose cutting it as part of a "
               "'reduce my spending' style goal.",
               _obj({}), RISK_READ)
def _t_budgets(ctx, args):
    fe = ctx.open_finance()
    try:
        budgets = fe.budget_status()
        for b in budgets:
            b["custodial_category"] = is_custodial_category(fe, b["category"])
        return budgets
    finally:
        fe.close()


@register_tool("list_subscriptions", "Tracked subscriptions.",
               _obj({"status": {"type": "string",
                                "description": "active (default) | cancelled | all"}}),
               RISK_READ)
def _t_subs(ctx, args):
    fe = ctx.open_finance()
    try:
        status = args.get("status") or "active"
        return fe.list_subscriptions(status=None if status == "all" else status)
    finally:
        fe.close()


@register_tool("list_accounts", "Bank accounts on file (id, name, bank, type).",
               _obj({}), RISK_READ)
def _t_accounts(ctx, args):
    fe = ctx.open_finance()
    try:
        return [{k: a.get(k) for k in ("id", "nickname", "bank_name", "account_type")}
                for a in fe.list_accounts()]
    finally:
        fe.close()


@register_tool("list_income", "Recurring income sources.", _obj({}), RISK_READ)
def _t_income(ctx, args):
    fe = ctx.open_finance()
    try:
        return fe.list_income_sources()
    finally:
        fe.close()


@register_tool("upcoming_bills", "Subscription renewals due within N days.",
               _obj({"days": {"type": "integer"}}), RISK_READ)
def _t_bills(ctx, args):
    fe = ctx.open_finance()
    try:
        return fe.upcoming_bills(days=int(args.get("days") or 30))
    finally:
        fe.close()


@register_tool("afford_check",
               "'Can I afford this?' — verdict with reasoning and risk level.",
               _obj({"amount": {"type": "number"},
                     "description": {"type": "string"}}, ["amount"]), RISK_READ)
def _t_afford(ctx, args):
    from ..finance.afford import can_afford
    fe = ctx.open_finance()
    try:
        return can_afford(float(args["amount"]), args.get("description") or "",
                          fe, collab_db=ctx.collab)
    finally:
        fe.close()


@register_tool("list_business_entities", "Registered side businesses.",
               _obj({}), RISK_READ)
def _t_entities(ctx, args):
    fe = ctx.open_finance()
    try:
        return fe.list_business_entities()
    finally:
        fe.close()


@register_tool("list_ledger_entries", "Ledger entries for one business entity.",
               _obj({"entity_id": {"type": "string"}}, ["entity_id"]), RISK_READ)
def _t_ledger(ctx, args):
    fe = ctx.open_finance()
    try:
        return fe.list_ledger_entries(args["entity_id"])
    finally:
        fe.close()


@register_tool("list_compliance_suggestions",
               "Compliance suggestions for one business entity.",
               _obj({"entity_id": {"type": "string"}}, ["entity_id"]), RISK_READ)
def _t_compliance_list(ctx, args):
    fe = ctx.open_finance()
    try:
        return fe.list_compliance_suggestions(args["entity_id"])
    finally:
        fe.close()


@register_tool("list_goals", "Goals overview with progress/blockers/tasks.",
               _obj({}), RISK_READ)
def _t_goals(ctx, args):
    from ..autonomous import GoalEngine
    return GoalEngine(ctx.collab).overview()


@register_tool("pending_approvals", "Actions waiting in the Approval Inbox.",
               _obj({}), RISK_READ)
def _t_pending(ctx, args):
    return [{k: p[k] for k in ("id", "title", "body", "action_type", "tier",
                               "created_at")}
            for p in ctx.store.list_approvals("pending")]


@register_tool("recent_events", "Recent events from the event bus.",
               _obj({"limit": {"type": "integer"},
                     "type": {"type": "string", "description": "filter by event type"}}),
               RISK_READ)
def _t_events(ctx, args):
    return ctx.events().recent(event_type=args.get("type"),
                               n=min(int(args.get("limit") or 20), 100))


@register_tool("recent_notifications", "Latest in-app notifications.",
               _obj({"limit": {"type": "integer"}}), RISK_READ)
def _t_notifs(ctx, args):
    return ctx.notify_store().list(limit=min(int(args.get("limit") or 10), 50))


@register_tool("graph_neighbors",
               "Knowledge-graph neighbors of a node (notes/goals/tasks/memories).",
               _obj({"node_id": {"type": "string"},
                     "rel": {"type": "string",
                             "description": "optional: depends_on|related_to|supports|blocks|belongs_to"}},
                    ["node_id"]), RISK_READ)
def _t_graph(ctx, args):
    from ..knowledge_graph.store import GraphStore
    g = GraphStore(str(_index_dir(ctx) / "graph.db"))
    try:
        return {"node": g.get_node(args["node_id"]),
                "neighbors": g.neighbors(args["node_id"], rel=args.get("rel"))}
    finally:
        g.conn.close()


def _user_vault(ctx) -> Path:
    # resolve the vault the user is ACTUALLY using (external/linked Obsidian
    # vault respected) — tenancy, not paths.vault_dir
    from ..saas import tenancy
    return tenancy.resolve_vault_dir(ctx.user_id)


@register_tool("search_captures",
               "Search the user's photo memory (pictures taken with the app: "
               "caption, text found in the photo, place, tags, user note). "
               "Use for questions like 'that poster I photographed in "
               "Bangalore'.",
               _obj({"query": {"type": "string"},
                     "limit": {"type": "integer", "description": "max results (<=10)"}},
                    ["query"]), RISK_READ)
def _t_search_captures(ctx, args):
    from .. import captures as captures_mod
    return captures_mod.search_captures(
        args["query"], vault=_user_vault(ctx),
        limit=min(int(args.get("limit") or 5), 10))


@register_tool("recent_captures",
               "Photos captured in the last N days (date, place, caption, "
               "text in photo, tags). Good for daily/weekly review questions.",
               _obj({"days": {"type": "integer", "description": "default 7"},
                     "limit": {"type": "integer"}}), RISK_READ)
def _t_recent_captures(ctx, args):
    from .. import captures as captures_mod
    days = max(int(args.get("days") or 7), 1)
    start = (_dt.date.today() - _dt.timedelta(days=days - 1)).isoformat()
    rows = captures_mod.captures_between(
        start, _dt.date.today().isoformat(), vault=_user_vault(ctx))
    return rows[:min(int(args.get("limit") or 20), 50)]


# ===========================================================================
# WRITE tools (agent-invoked calls park in the approval queue once R3 gates)
# ===========================================================================

@register_tool("add_transaction",
               "Record one manual transaction. Negative amount = expense.",
               _obj({"amount": {"type": "number"},
                     "category": {"type": "string"},
                     "merchant": {"type": "string"},
                     "date": {"type": "string", "description": "YYYY-MM-DD, default today"},
                     "notes": {"type": "string"},
                     "account_id": {"type": "string"}},
                    ["amount", "category"]), RISK_WRITE)
def _t_add_txn(ctx, args):
    from ..automation import executors
    return executors.execute(ctx, "add_transaction", args)


@register_tool("set_budget",
               "Create/update a monthly budget cap for a category. Before "
               "proposing a cut as part of a spending-reduction goal, check "
               "list_budgets' custodial_category flag for this category — "
               "if true, it is pass-through money forwarded to "
               "beneficiaries, not personal spending, and should not be "
               "targeted for a spending cut.",
               _obj({"category": {"type": "string"},
                     "monthly_limit": {"type": "number"}},
                    ["category", "monthly_limit"]), RISK_WRITE)
def _t_set_budget(ctx, args):
    from ..automation import executors
    return executors.execute(ctx, "set_budget", args)


@register_tool("add_subscription", "Track a recurring subscription.",
               _obj({"name": {"type": "string"},
                     "monthly_cost": {"type": "number"},
                     "annual_cost": {"type": "number"},
                     "renewal_date": {"type": "string"},
                     "payment_method": {"type": "string"}}, ["name"]), RISK_WRITE)
def _t_add_sub(ctx, args):
    from ..automation import executors
    return executors.execute(ctx, "add_subscription", args)


@register_tool("add_income", "Record a recurring income source.",
               _obj({"name": {"type": "string"},
                     "amount": {"type": "number"},
                     "type": {"type": "string", "description": "salary|freelance|other"},
                     "recurrence": {"type": "string", "description": "monthly|yearly|weekly"}},
                    ["name", "amount"]), RISK_WRITE)
def _t_add_income(ctx, args):
    fe = ctx.open_finance()
    try:
        sid = fe.add_income_source(
            name=args["name"], income_type=args.get("type") or "salary",
            amount=float(args["amount"]),
            recurrence=args.get("recurrence") or "monthly")
    finally:
        fe.close()
    try:
        # fire-and-forget emission (_emit_fin pattern): a bad event must not
        # fail the write that already happened
        ctx.events().emit("finance.income_added",
                          {"name": args["name"], "amount": float(args["amount"]),
                           "source": "tool"}, source="tools")
    except Exception:
        pass
    return {"id": sid}


@register_tool("add_ledger_entry",
               "Post one entry to a business entity's ledger (with event provenance).",
               _obj({"entity_id": {"type": "string"},
                     "date": {"type": "string", "description": "YYYY-MM-DD"},
                     "amount": {"type": "number"},
                     "description": {"type": "string"},
                     "category": {"type": "string"}},
                    ["entity_id", "date", "amount"]), RISK_WRITE)
def _t_add_ledger(ctx, args):
    fe = ctx.open_finance()
    try:
        if fe.get_business_entity(args["entity_id"]) is None:
            raise ToolError(f"business entity {args['entity_id']!r} not found")
        # provenance first: ledger rows require a source event id (NOT NULL)
        eid = ctx.events().emit("finance.ledger_entry_posted", {
            "entity_id": args["entity_id"], "amount": float(args["amount"]),
            "date": args["date"], "description": args.get("description", ""),
            "posted_by": "tool"}, source="tools")
        lid = fe.add_ledger_entry(
            business_entity_id=args["entity_id"], date=args["date"],
            amount=float(args["amount"]), source_event_id=eid,
            description=args.get("description", ""),
            category=args.get("category") or "Uncategorized",
            posted_by="tool")
        return {"id": lid, "source_event_id": eid}
    finally:
        fe.close()


@register_tool("run_compliance",
               "Generate compliance suggestions for a business entity's "
               "unprocessed ledger entries.",
               _obj({"entity_id": {"type": "string"}}, ["entity_id"]), RISK_WRITE)
def _t_run_compliance(ctx, args):
    from ..finance.business.compliance import generate_suggestions
    fe = ctx.open_finance()
    try:
        entity = fe.get_business_entity(args["entity_id"])
        if entity is None:
            raise ToolError(f"business entity {args['entity_id']!r} not found")
        return {"suggestions": generate_suggestions(fe, entity, ctx.llm)}
    finally:
        fe.close()


@register_tool("vault_write_note",
               "Write an atomic markdown note into the user's vault (idempotent).",
               _obj({"title": {"type": "string"},
                     "content": {"type": "string"},
                     "type": {"type": "string", "description": "note label, default 'agent'"}},
                    ["title", "content"]), RISK_WRITE)
def _t_vault_note(ctx, args):
    from ..memory.writer import MemoryWriter
    vault = _user_vault(ctx)   # linked cloud vault, not the internal folder
    vault.mkdir(parents=True, exist_ok=True)
    eid = uuid.uuid4().hex[:12]
    p = MemoryWriter(vault).write_atomic(
        args.get("type") or "agent", args["title"], args["content"], eid)
    return {"path": str(p) if p else None, "eid": eid, "written": p is not None}


@register_tool("emit_event", "Emit a custom event onto the event bus.",
               _obj({"type": {"type": "string"},
                     "payload": {"type": "object"}}, ["type"]), RISK_WRITE)
def _t_emit(ctx, args):
    eid = ctx.events().emit(args["type"], args.get("payload") or {}, source="tools")
    return {"event_id": eid}


@register_tool("zakat_status",
               "Full zakat report: live gold/silver nisab, wealth breakdown "
               "(custodial excluded), hawl on the Hijri calendar, liability.",
               _obj({"jurisdiction": {"type": "string"}}), RISK_READ)
def _t_zakat(ctx, args):
    from ..obligations.zakat import zakat_report
    from pathlib import Path
    fe = ctx.open_finance()
    try:
        return zakat_report(fe, (args.get("jurisdiction") or "india").lower(),
                            cache_dir=Path(ctx.finance_path).parent)
    finally:
        fe.close()


@register_tool("create_goal",
               "Create a new goal (returns its id — use it for add_goal_task).",
               _obj({"title": {"type": "string"},
                     "domain": {"type": "string"}}, ["title"]), RISK_WRITE)
def _t_create_goal(ctx, args):
    from ..autonomous import GoalEngine
    gid = GoalEngine(ctx.collab).create_goal(
        args["title"], domain=args.get("domain") or "general")
    return {"id": gid}


@register_tool("add_goal_task", "Add a task under an existing goal "
               "(goal_id must come from list_goals or create_goal — never invent it).",
               _obj({"goal_id": {"type": "string"},
                     "title": {"type": "string"}}, ["goal_id", "title"]), RISK_WRITE)
def _t_add_task(ctx, args):
    from ..autonomous import GoalEngine
    # Found via manual testing: the orchestrator hallucinated a goal_id and
    # this inserted an orphan task no UI ever showed. Validate the FK.
    row = ctx.collab.conn.execute(
        "SELECT id FROM goals WHERE id=?", (args["goal_id"],)).fetchone()
    if row is None:
        raise ValueError(
            f"goal '{args['goal_id']}' does not exist — call list_goals for the "
            "real id, or create_goal to make one first")
    return {"id": GoalEngine(ctx.collab).add_task(args["goal_id"], args["title"])}


@register_tool("approve_action",
               "Approve and execute a pending Approval Inbox item. "
               "Only a human actor may call this.",
               _obj({"approval_id": {"type": "string"}}, ["approval_id"]), RISK_WRITE)
def _t_approve(ctx, args):
    from ..automation import executors
    if ctx._extras.get("tool_actor") != "human":
        raise ToolError("approve_action is human-only — an agent cannot "
                        "approve its own proposals")
    return executors.approve(ctx, args["approval_id"])


@register_tool("reject_action", "Reject a pending Approval Inbox item (human-only).",
               _obj({"approval_id": {"type": "string"},
                     "reason": {"type": "string"}}, ["approval_id"]), RISK_WRITE)
def _t_reject(ctx, args):
    from ..automation import executors
    if ctx._extras.get("tool_actor") != "human":
        raise ToolError("reject_action is human-only")
    return executors.reject(ctx, args["approval_id"], args.get("reason") or "")


@register_tool("run_automation_job", "Trigger one automation job immediately.",
               _obj({"name": {"type": "string"}}, ["name"]), RISK_WRITE)
def _t_run_job(ctx, args):
    from ..automation import jobs
    if args["name"] not in jobs.HANDLERS:
        raise ToolError(f"unknown job {args['name']!r} — valid: "
                        + ", ".join(sorted(jobs.HANDLERS)))
    return jobs.run_job(ctx, args["name"])


# ===========================================================================
# DESTRUCTIVE tools
# ===========================================================================

@register_tool("delete_transaction", "Delete one transaction by id.",
               _obj({"tid": {"type": "string"}}, ["tid"]), RISK_DESTRUCTIVE)
def _t_del_txn(ctx, args):
    fe = ctx.open_finance()
    try:
        return {"deleted": fe.delete_transaction(args["tid"])}
    finally:
        fe.close()


@register_tool("delete_subscription", "Delete one subscription by id.",
               _obj({"sid": {"type": "string"}}, ["sid"]), RISK_DESTRUCTIVE)
def _t_del_sub(ctx, args):
    fe = ctx.open_finance()
    try:
        return {"deleted": fe.delete_subscription(args["sid"])}
    finally:
        fe.close()


@register_tool("delete_budget", "Remove a budget cap for a category.",
               _obj({"category": {"type": "string"}}, ["category"]), RISK_DESTRUCTIVE)
def _t_del_budget(ctx, args):
    fe = ctx.open_finance()
    try:
        return {"deleted": fe.delete_budget(args["category"])}
    finally:
        fe.close()
