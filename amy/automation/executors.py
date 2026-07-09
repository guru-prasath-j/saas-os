"""Approval Inbox action executors + the tier router.

Every automated *write* in the system flows through submit_action() with an
autonomy tier — never straight into the DB from a job. That gives one place
to enforce the safety rails:
  - tier 2 actions (money movement, anything the user must sanction) are
    parked as pending approvals and only run via approve().
  - tiers 0/1 execute immediately but still leave an approval row
    (status='auto_executed') so every automated action is auditable.
  - no executor deletes data; imports dedup before insert so an approved
    action re-running is harmless.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path

from .store import AutomationStore


# ---------------------------------------------------------------------------
# Job / executor context
# ---------------------------------------------------------------------------

@dataclass
class JobCtx:
    user_id: str
    user_email: str
    finance_path: str
    collab: object                     # open CollabDB (caller closes)
    store: AutomationStore
    connector_dir: Path
    llm: object | None = None          # TrackedLLM (drop-in for LLMRouter)
    _extras: dict = field(default_factory=dict)

    def open_finance(self):
        from ..finance.engine import FinanceEngine
        return FinanceEngine(self.finance_path)

    def events(self):
        """EventStore with reactive agents wired on (agents also react to
        job-driven imports). Uses amy.events.factory.get_events() with THIS
        ctx reused (Part 0 / quirk 20 fix) — register_reactive_agents is
        idempotent per-instance, so calling this more than once per run is
        safe, but each call still builds a fresh EventStore. Wiring failures
        degrade to a bare store."""
        if self._extras.get("no_reactive_agents"):
            from ..events.store import EventStore
            return EventStore(self.collab)
        from ..events.factory import get_events
        return get_events(self.user_id, self.collab, ctx=self)

    def notify_store(self):
        from ..notifications import NotificationStore
        return NotificationStore(self.collab)

    def google_creds(self):
        try:
            from ..connectors.google import load_credentials, TOKEN_FILENAME
            token_path = self.connector_dir / TOKEN_FILENAME
            if not token_path.exists():
                return None
            return load_credentials(str(token_path))
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Executor registry
# ---------------------------------------------------------------------------

EXECUTORS: dict[str, callable] = {}


def register(action_type: str):
    def deco(fn):
        EXECUTORS[action_type] = fn
        return fn
    return deco


def execute(ctx: JobCtx, action_type: str, payload: dict) -> dict:
    fn = EXECUTORS.get(action_type)
    if fn is None:
        raise ValueError(f"no executor registered for action {action_type!r}")
    return fn(ctx, payload)


def submit_action(ctx: JobCtx, tier: int, action_type: str, title: str,
                  body: str = "", payload: dict | None = None,
                  source: str = "", dedup_key: str | None = None,
                  reasoning: str = "", risk: str = "",
                  affected_entity: str = "",
                  expires_at: str | None = None) -> dict:
    """Tier router: tier<=1 executes now (1 also notifies); tier 2 parks
    a pending approval + notification. Returns {approval_id, status, result?}."""
    payload = payload or {}

    if tier >= 2:
        aid = ctx.store.create_approval(
            tier=tier, action_type=action_type, title=title, body=body,
            payload=payload, source=source, status="pending", dedup_key=dedup_key,
            reasoning=reasoning, risk=risk, affected_entity=affected_entity,
            expires_at=expires_at)
        if aid is None:
            return {"approval_id": None, "status": "duplicate"}
        try:
            ns = ctx.notify_store()
            if not ns.exists_today("approval_needed", aid):
                ns.create(type="approval_needed",
                          title=f"Approval needed: {title}",
                          body=body or "Review this proposed action in the Approval Inbox.",
                          priority="high",
                          related_entity={"entity_type": "approval", "id": aid,
                                          "action_type": action_type})
        except Exception:
            pass
        return {"approval_id": aid, "status": "pending"}

    # tier 0/1 — execute immediately, still audit-logged as an approval row
    try:
        result = execute(ctx, action_type, payload)
        status = "auto_executed"
    except Exception as exc:
        result = {"error": str(exc)}
        status = "failed"
    aid = ctx.store.create_approval(
        tier=tier, action_type=action_type, title=title, body=body,
        payload=payload, source=source, status=status, dedup_key=dedup_key,
        result=result, reasoning=reasoning, risk=risk,
        affected_entity=affected_entity)
    if aid is None:
        return {"approval_id": None, "status": "duplicate"}
    if tier == 1 and status == "auto_executed":
        try:
            ns = ctx.notify_store()
            ns.create(type="automation_action", title=title,
                      body=body or "Done automatically by the automation layer.",
                      priority="normal",
                      related_entity={"entity_type": "approval", "id": aid,
                                      "action_type": action_type})
        except Exception:
            pass
    return {"approval_id": aid, "status": status, "result": result}


def _clear_approval_notification(ctx: JobCtx, approval_id: str) -> None:
    """Bug found via manual testing: rejecting/approving an item in the
    Approval Inbox left its 'approval needed' bell notification unread
    forever — the badge stayed stuck even after the user had already acted
    on it. Best-effort; never blocks the decision itself."""
    try:
        ctx.notify_store().mark_read_by_related_id(approval_id)
    except Exception:
        pass


def approve(ctx: JobCtx, approval_id: str) -> dict:
    """Execute a pending tier-2 approval. Records the decision for learning."""
    ap = ctx.store.get_approval(approval_id)
    if not ap:
        raise ValueError("approval not found")
    if ap["status"] != "pending":
        raise ValueError(f"approval is {ap['status']}, not pending")
    try:
        result = execute(ctx, ap["action_type"], ap["payload"])
        ctx.store.set_approval_status(approval_id, "executed", result)
        status = "executed"
    except Exception as exc:
        result = {"error": str(exc)}
        ctx.store.set_approval_status(approval_id, "failed", result)
        status = "failed"
    _record_decision(ctx, ap, approved=True)
    _clear_approval_notification(ctx, approval_id)
    return {"status": status, "result": result}


def reject(ctx: JobCtx, approval_id: str, reason: str = "") -> dict:
    ap = ctx.store.get_approval(approval_id)
    if not ap:
        raise ValueError("approval not found")
    if ap["status"] != "pending":
        raise ValueError(f"approval is {ap['status']}, not pending")
    ctx.store.set_approval_status(approval_id, "rejected", {"reason": reason})
    _record_decision(ctx, ap, approved=False, reason=reason)
    _clear_approval_notification(ctx, approval_id)
    return {"status": "rejected"}


def _record_decision(ctx: JobCtx, approval: dict, approved: bool, reason: str = ""):
    """Feed approve/reject choices into the decision journal so the system
    can learn the user's thresholds over time."""
    try:
        from ..engines import DecisionEngine
        DecisionEngine(ctx.collab, events=ctx.events()).record(
            f"{'Approved' if approved else 'Rejected'}: {approval['title']}",
            category="automation",
            reason=reason or f"action={approval['action_type']} tier={approval['tier']}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------

@register("import_statement")
def _exec_import_statement(ctx: JobCtx, payload: dict) -> dict:
    """Insert parsed statement transactions (dedup-protected). Payload:
    {account_id, bank_name, filename, transactions:[{date,description,amount,category?}],
     column_map?, save_column_map?}"""
    from ..finance.sync.pdf_import import _is_near_duplicate

    account_id = payload["account_id"]
    txns = payload.get("transactions") or []
    fe = ctx.open_finance()
    try:
        acc = fe.get_account(account_id)
        if acc is None:
            raise ValueError(f"account {account_id!r} not found")
        imported = skipped = 0
        for t in txns:
            date = t.get("date")
            amount = t.get("amount")
            desc = t.get("description") or t.get("merchant") or ""
            if not date or amount is None:
                skipped += 1
                continue
            exists = fe.conn.execute(
                "SELECT id FROM transactions"
                " WHERE date=? AND amount=? AND merchant=? AND account_id=? LIMIT 1",
                (date, amount, desc, account_id)).fetchone()
            if exists or _is_near_duplicate(fe, date, amount, account_id, desc):
                skipped += 1
                continue
            fe.add_transaction(
                amount=amount, category=t.get("category") or "Uncategorized",
                merchant=desc, date=date, source="auto_ingest",
                account_id=account_id)
            imported += 1
        if payload.get("save_column_map") and payload.get("column_map") \
                and payload.get("bank_name"):
            fe.save_column_map(payload["bank_name"], payload["column_map"])
        try:
            ctx.events().emit("finance.csv_imported", {
                "bank_name": payload.get("bank_name", ""),
                "imported": imported, "skipped": skipped,
                "source": "auto_ingest", "filename": payload.get("filename", ""),
            }, source="automation")
        except Exception:
            pass
        return {"imported": imported, "skipped": skipped}
    finally:
        fe.close()


@register("custodial_disburse")
def _exec_custodial_disburse(ctx: JobCtx, payload: dict) -> dict:
    """Record an approved custodial disbursement cycle (one txn per beneficiary)
    and mirror each row to the Google Sheet. Payload:
    {account_id, date?, category?, disbursements:[{beneficiary_id,name,amount,mode?,notes?}]}"""
    from ..finance.custodial_sheets import append_disbursement_row

    account_id = payload["account_id"]
    date = payload.get("date") or _dt.date.today().isoformat()
    category = payload.get("category") or "Custodial Disbursement"
    fe = ctx.open_finance()
    try:
        acc = fe.get_account(account_id)
        if not acc or acc.get("account_type") != "custodial":
            raise ValueError("custodial account not found")
        creds = ctx.google_creds()
        events = ctx.events()
        rows = []
        for d in payload.get("disbursements") or []:
            ben = fe.get_beneficiary(d["beneficiary_id"])
            if not ben or ben["account_id"] != account_id:
                rows.append({"beneficiary_id": d.get("beneficiary_id"),
                             "error": "beneficiary not found"})
                continue
            amount = abs(float(d["amount"]))
            mode = d.get("mode") or "NEFT"
            notes = d.get("notes") or ""
            tid = fe.add_transaction(
                amount=-amount, category=category, merchant=ben["name"],
                date=date, source="custodial_autopilot", notes=notes,
                account_id=account_id)
            fe.conn.execute("UPDATE transactions SET beneficiary_id=? WHERE id=?",
                            (d["beneficiary_id"], tid))
            fe.conn.commit()
            events.emit("custodial.disbursed", {
                "account_id": account_id, "beneficiary_id": d["beneficiary_id"],
                "beneficiary_name": ben["name"], "transaction_id": tid,
                "amount": amount, "date": date, "mode": mode,
            }, source="custodial_autopilot")
            sheet = append_disbursement_row(
                creds, acc, ben, date, mode, amount, category, notes)
            rows.append({"beneficiary": ben["name"], "transaction_id": tid,
                         "amount": amount, "sheet_write": sheet})
        return {"date": date, "disbursed": rows,
                "balance": fe.custodial_balance(account_id)}
    finally:
        fe.close()


@register("add_subscription")
def _exec_add_subscription(ctx: JobCtx, payload: dict) -> dict:
    fe = ctx.open_finance()
    try:
        sid = fe.add_subscription(
            name=payload["name"],
            monthly_cost=float(payload.get("monthly_cost") or 0),
            annual_cost=float(payload.get("annual_cost") or 0),
            renewal_date=payload.get("renewal_date"),
            payment_method=payload.get("payment_method", ""))
        try:
            ctx.events().emit("finance.subscription_added",
                              {"name": payload["name"], "source": "automation"},
                              source="automation")
        except Exception:
            pass
        return {"id": sid}
    finally:
        fe.close()


@register("set_budget")
def _exec_set_budget(ctx: JobCtx, payload: dict) -> dict:
    fe = ctx.open_finance()
    try:
        fe.set_budget(payload["category"], float(payload["monthly_limit"]))
        try:
            ctx.events().emit("finance.budget_set", {
                "category": payload["category"],
                "monthly_limit": float(payload["monthly_limit"]),
                "source": "automation"}, source="automation")
        except Exception:
            pass
        return {"category": payload["category"],
                "monthly_limit": float(payload["monthly_limit"])}
    finally:
        fe.close()


# ---------------------------------------------------------------------------
# R3: agent gate — every registry write/destructive tool invoked by an agent
# parks in the Approval Inbox instead of executing.
# ---------------------------------------------------------------------------

# Which entity-ish arg names identify what an action touches (for the queue UI)
_ENTITY_ARGS = ("entity_id", "account_id", "approval_id", "tid", "sid",
                "goal_id", "category", "name", "title")

_DEFAULT_EXPIRY_DAYS = 7


def _tier_for(risk: str, external: bool = False) -> int:
    """Explicit tier policy for AGENT-initiated actions.

    destructive is hard-pinned to tier 2 — no config can lower it.
    external=True (a GitHub comment, a Plane task create/update: anything
    sent to a third-party system — see the tool's extras={"external": True},
    CONNECTOR COMPLETION Part 1) is ALSO hard-pinned to tier 2, same as
    destructive: these sends are irreversible once delivered, so
    AMY_AGENT_WRITE_TIER must never soften them the way it can for ordinary
    internal writes.
    write (non-external) defaults to tier 2 (park for approval);
    AMY_AGENT_WRITE_TIER=1 restores execute-then-notify for installs that
    want it.
    """
    if risk == "destructive" or external:
        return 2
    from .. import config
    try:
        t = int(config._env("AMY_AGENT_WRITE_TIER", "2"))
    except ValueError:
        t = 2
    return min(max(t, 0), 2)


def _custodial_budget_warning(ctx: JobCtx, tool, args: dict) -> str | None:
    """Guardrail found via manual testing: the orchestrator proposed cutting
    a 'Custodial Disbursement' budget as if it were personal spending. This
    injects a visible warning onto the approval card whenever an agent
    targets set_budget at a category that is actually custodial pass-through
    money — even if the LLM ignored the tool's own description. Read-only
    check; never touches custodial.py's disbursement/refill logic."""
    if tool.name != "set_budget":
        return None
    category = args.get("category")
    if not category:
        return None
    try:
        from ..tools.builtin import is_custodial_category
        fe = ctx.open_finance()
        try:
            if is_custodial_category(fe, category):
                return (f"Category '{category}' is mostly custodial "
                        "pass-through money (forwarded to beneficiaries), "
                        "not the user's own discretionary spending — "
                        "review carefully before treating this as a "
                        "personal spending cut.")
        finally:
            fe.close()
    except Exception:
        return None   # warning is best-effort; never block the proposal
    return None


def agent_gate(ctx: JobCtx, tool, args: dict) -> dict:
    """Installed as amy.tools.registry.AGENT_GATE (see amy/automation/__init__).

    Agents set ctx._extras['agent_name'] / ['agent_reasoning'] before
    invoking a tool so the queue entry carries who proposed it and why.
    """
    reasoning = str(ctx._extras.get("agent_reasoning") or
                    "Proposed autonomously by an agent (no reasoning supplied).")
    warning = _custodial_budget_warning(ctx, tool, args)
    if warning:
        reasoning = f"⚠️ {warning}\n\n{reasoning}"
    agent = str(ctx._extras.get("agent_name") or "agent")
    dedup_key = ctx._extras.pop("agent_dedup_key", None)
    affected = next((f"{k}={args[k]}" for k in _ENTITY_ARGS if args.get(k)), "")
    expires = (_dt.datetime.now(_dt.timezone.utc)
               + _dt.timedelta(days=_DEFAULT_EXPIRY_DAYS)).isoformat()
    return submit_action(
        ctx,
        tier=_tier_for(tool.risk, external=bool(tool.extras.get("external"))),
        action_type="tool_call",
        title=f"{agent}: {tool.name}",
        body=reasoning,
        payload={"tool": tool.name, "args": args},
        source=agent,
        dedup_key=dedup_key,
        reasoning=reasoning,
        risk=tool.risk,
        affected_entity=affected,
        expires_at=expires)


@register("tool_call")
def _exec_tool_call(ctx: JobCtx, payload: dict) -> dict:
    """Execute an approved (or tier<=1) registry tool call. Runs the tool
    handler directly — approval IS the human consent, so no re-gating, and
    the handler sees a human actor."""
    from ..tools import get_tool, validate_args
    tool = get_tool(payload["tool"])
    args = validate_args(tool, payload.get("args") or {})
    ctx._extras["tool_actor"] = "human"
    return tool.handler(ctx, args)


@register("external_draft")
def _exec_external_draft(ctx: JobCtx, payload: dict) -> dict:
    """Universal inbox (CONTEXT_PLAN C6): the approval decision IS the product.
    Nothing executes here — the external system that proposed the draft polls
    GET /api/inbox/decisions and acts only on rows a human approved."""
    return {"acknowledged": True, "draft": payload.get("draft_id") or ""}


@register("add_task")
def _exec_add_task(ctx: JobCtx, payload: dict) -> dict:
    """Create a collab task, optionally place-tagged so the errand agent
    reminds about it on arrival (CONTEXT_PLAN C4 — approved pattern task)."""
    import uuid as _uuid_mod
    tid = _uuid_mod.uuid4().hex[:12]
    ctx.collab.conn.execute(
        "INSERT INTO tasks (id, goal_id, title, done, created_at, place_tag)"
        " VALUES (?,?,?,0,?,?)",
        (tid, payload.get("goal_id") or "", str(payload["title"]).strip(),
         _dt.datetime.now(_dt.timezone.utc).isoformat(),
         (payload.get("place_tag") or "").strip().lower()))
    ctx.collab.conn.commit()
    return {"task_id": tid}


@register("add_place")
def _exec_add_place(ctx: JobCtx, payload: dict) -> dict:
    """Create a geofence place (CONTEXT_PLAN C2 — approved learned place)."""
    from ..geo import GeoStore
    gs = GeoStore(ctx.collab)
    pid = gs.add_place(
        payload["name"], float(payload["lat"]), float(payload["lon"]),
        kind=payload.get("kind") or "",
        radius_m=int(payload.get("radius_m") or 150),
        source=payload.get("source") or "learned")
    return {"place_id": pid}


@register("add_transaction")
def _exec_add_transaction(ctx: JobCtx, payload: dict) -> dict:
    fe = ctx.open_finance()
    try:
        tid = fe.add_transaction(
            amount=float(payload["amount"]),
            category=payload.get("category") or "Uncategorized",
            merchant=payload.get("merchant", ""),
            date=payload.get("date"),
            source=payload.get("source", "assistant"),
            notes=payload.get("notes", ""),
            account_id=payload.get("account_id"))
        return {"id": tid}
    finally:
        fe.close()


# ---------------------------------------------------------------------------
# External connector writes (CONNECTOR COMPLETION Part 1) — GitHub/Plane.
# These are the execution backend for github_comment/plane_create_task/
# plane_update_task (amy/tools/connector_tools.py), whose tool.extras has
# external=True — agent_gate's _tier_for() hard-pins them to tier 2, so this
# code only runs for a human-actor direct call or an approved (human-
# consented) tier-2 approval, exactly like tool_call above. Candidate remote
# tool names are duplicated (not imported) from amy/tools/connector_tools.py
# on purpose — importing a leading-underscore name across the
# tools<->automation module boundary would couple two packages that only
# ever talk to each other through the registry/executor indirection.
# ---------------------------------------------------------------------------

_GH_COMMENT = ("add_issue_comment", "create_issue_comment")
_PLANE_CREATE_TASK = ("create_work_item", "create_issue")
_PLANE_UPDATE_TASK = ("update_work_item", "update_issue")


@register("github_comment")
def _exec_github_comment(ctx: JobCtx, payload: dict) -> dict:
    from ..connectors.mcp_call import call_mcp_tool
    call_args = {k: payload[k] for k in ("owner", "repo") if payload.get(k)}
    call_args["issue_number"] = payload["number"]
    call_args["body"] = payload["body"]
    return call_mcp_tool(ctx.user_id, ctx.store, "github", _GH_COMMENT, call_args)


@register("plane_create_task")
def _exec_plane_create_task(ctx: JobCtx, payload: dict) -> dict:
    from ..connectors.mcp_call import call_mcp_tool
    call_args = {"name": payload["title"], "title": payload["title"]}
    if payload.get("description"):
        call_args["description"] = payload["description"]
    if payload.get("project_id"):
        call_args["project_id"] = payload["project_id"]
    return call_mcp_tool(ctx.user_id, ctx.store, "plane", _PLANE_CREATE_TASK,
                         call_args, target_style="single")


@register("plane_update_task")
def _exec_plane_update_task(ctx: JobCtx, payload: dict) -> dict:
    from ..connectors.mcp_call import call_mcp_tool
    call_args = {"issue_id": payload["task_id"], "work_item_id": payload["task_id"]}
    if payload.get("state"):
        call_args["state"] = payload["state"]
    if payload.get("title"):
        call_args["name"] = payload["title"]
    if payload.get("project_id"):
        call_args["project_id"] = payload["project_id"]
    return call_mcp_tool(ctx.user_id, ctx.store, "plane", _PLANE_UPDATE_TASK,
                         call_args, target_style="single")
