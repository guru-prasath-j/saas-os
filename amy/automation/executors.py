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
        job-driven imports). Wiring failures degrade to a bare store."""
        from ..events.store import EventStore
        es = EventStore(self.collab)
        if not self._extras.get("no_reactive_agents"):
            try:
                from ..agents.reactive import register_reactive_agents
                register_reactive_agents(es, self)
            except Exception:
                pass
        return es

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
    return {"status": status, "result": result}


def reject(ctx: JobCtx, approval_id: str, reason: str = "") -> dict:
    ap = ctx.store.get_approval(approval_id)
    if not ap:
        raise ValueError("approval not found")
    if ap["status"] != "pending":
        raise ValueError(f"approval is {ap['status']}, not pending")
    ctx.store.set_approval_status(approval_id, "rejected", {"reason": reason})
    _record_decision(ctx, ap, approved=False, reason=reason)
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


def _tier_for(risk: str) -> int:
    """Explicit tier policy for AGENT-initiated actions.

    destructive is hard-pinned to tier 2 — no config can lower it.
    write defaults to tier 2 (park for approval); AMY_AGENT_WRITE_TIER=1
    restores execute-then-notify for installs that want it.
    """
    if risk == "destructive":
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
        tier=_tier_for(tool.risk),
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
