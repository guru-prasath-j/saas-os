"""AI assistant console (Phase 4) — one chat endpoint that can operate the app.

A small deterministic tool loop over the internal engines (no HTTP round-trip):
the LLM is shown a tool catalog and must answer with strict JSON — either
{"tool": "...", "args": {...}} to act, or {"final": "..."} to reply. Tool
results are fed back until it produces a final answer (max 6 steps).

Safety: only additive/reversible tools are exposed. There is no delete tool,
and anything tier-2 (custodial money, statement imports) is reachable ONLY by
approving an existing Approval Inbox item — the assistant cannot mint new
money-moving actions on its own.
"""
from __future__ import annotations

import json
import re

from . import executors, jobs
from .executors import JobCtx

_MAX_STEPS = 6

TOOLS: dict[str, dict] = {
    "finance_overview": {
        "desc": "Snapshot: income, month spend, balance estimate, budgets.",
        "args": {},
    },
    "list_transactions": {
        "desc": "Recent transactions. Optional filters.",
        "args": {"limit": "int<=50", "category": "str?", "since": "YYYY-MM-DD?"},
    },
    "list_budgets": {"desc": "All budget caps with current status.", "args": {}},
    "set_budget": {
        "desc": "Create/update a monthly budget cap for a category.",
        "args": {"category": "str", "monthly_limit": "float"},
    },
    "list_subscriptions": {"desc": "Active subscriptions.", "args": {}},
    "list_accounts": {"desc": "Bank accounts on file.", "args": {}},
    "add_transaction": {
        "desc": "Record one manual transaction (negative amount = expense).",
        "args": {"amount": "float", "category": "str", "merchant": "str",
                 "date": "YYYY-MM-DD?"},
    },
    "afford": {
        "desc": "'Can I afford this?' check.",
        "args": {"amount": "float", "description": "str"},
    },
    "list_goals": {"desc": "Goals overview with progress and blockers.", "args": {}},
    "pending_approvals": {
        "desc": "Actions waiting in the Approval Inbox.", "args": {}},
    "approve_action": {
        "desc": "Approve and execute a pending approval by id (user consent "
                "comes from this chat instruction).",
        "args": {"approval_id": "str"},
    },
    "reject_action": {
        "desc": "Reject a pending approval by id.",
        "args": {"approval_id": "str", "reason": "str?"},
    },
    "recent_notifications": {
        "desc": "Latest alerts/notifications.", "args": {"limit": "int<=10"}},
    "run_automation_job": {
        "desc": "Trigger one automation job now. Names: "
                + ", ".join(n for n, _ in jobs.DEFAULT_JOBS),
        "args": {"name": "str"},
    },
}


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _call_tool(ctx: JobCtx, name: str, args: dict):
    fe = ctx.open_finance()
    try:
        if name == "finance_overview":
            return fe.overview()
        if name == "list_transactions":
            return fe.list_transactions(
                limit=min(int(args.get("limit") or 20), 50),
                category=args.get("category"), since=args.get("since"))
        if name == "list_budgets":
            return fe.budget_status()
        if name == "set_budget":
            return executors.execute(ctx, "set_budget", {
                "category": args["category"],
                "monthly_limit": args["monthly_limit"]})
        if name == "list_subscriptions":
            return fe.list_subscriptions(status="active")
        if name == "list_accounts":
            return [{k: a.get(k) for k in
                     ("id", "nickname", "bank_name", "account_type")}
                    for a in fe.list_accounts()]
        if name == "add_transaction":
            return executors.execute(ctx, "add_transaction", args)
        if name == "afford":
            from ..finance.afford import can_afford
            return can_afford(float(args["amount"]),
                              str(args.get("description") or ""), fe,
                              collab_db=ctx.collab)
        if name == "list_goals":
            from ..autonomous import GoalEngine
            return GoalEngine(ctx.collab).overview()
        if name == "pending_approvals":
            return [{k: p[k] for k in
                     ("id", "title", "body", "action_type", "tier", "created_at")}
                    for p in ctx.store.list_approvals("pending")]
        if name == "approve_action":
            return executors.approve(ctx, args["approval_id"])
        if name == "reject_action":
            return executors.reject(ctx, args["approval_id"],
                                    args.get("reason") or "rejected via assistant")
        if name == "recent_notifications":
            return ctx.notify_store().list(
                limit=min(int(args.get("limit") or 5), 10))
        if name == "run_automation_job":
            return jobs.run_job(ctx, str(args.get("name") or ""))
        raise ValueError(f"unknown tool {name!r}")
    finally:
        fe.close()


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def _system_prompt() -> str:
    catalog = "\n".join(
        f"- {name}({', '.join(f'{a}: {t}' for a, t in spec['args'].items())}) — {spec['desc']}"
        for name, spec in TOOLS.items())
    return (
        "You are Amy, the user's personal-finance operating assistant. You can "
        "call tools to read data and take safe actions. Amounts are INR (₹).\n\n"
        f"Tools:\n{catalog}\n\n"
        "Respond with EXACTLY ONE JSON object, no prose around it and never "
        "more than one tool call per response:\n"
        '  {"tool": "<name>", "args": {...}}   to call a tool\n'
        '  {"final": "<answer for the user>"}  when done\n'
        "Call tools to get real numbers before answering; never invent data. "
        "Keep the final answer short and concrete."
    )


def _parse_step(raw: str) -> dict:
    """Take the FIRST complete JSON object in the response — models sometimes
    emit several tool calls at once; we execute one per turn."""
    raw = re.sub(r"```(?:json)?", "", raw or "").strip()
    decoder = json.JSONDecoder()
    idx = raw.find("{")
    while idx != -1:
        try:
            obj, _end = decoder.raw_decode(raw, idx)
        except Exception:
            idx = raw.find("{", idx + 1)
            continue
        if isinstance(obj, dict) and ("tool" in obj or "final" in obj):
            return obj
        idx = raw.find("{", idx + 1)
    return {"final": raw.strip() or "I couldn't produce an answer."}


def chat(ctx: JobCtx, message: str, history: list[dict] | None = None) -> dict:
    """Run the tool loop. Returns {reply, steps: [{tool, args, result}...]}"""
    if ctx.llm is None:
        return {"reply": "No LLM provider is available right now.", "steps": []}

    transcript: list[str] = []
    for h in (history or [])[-6:]:
        role = h.get("role", "user")
        transcript.append(f"{role}: {h.get('content', '')}")
    transcript.append(f"user: {message}")

    steps: list[dict] = []
    for _ in range(_MAX_STEPS):
        prompt = "\n".join(transcript) + "\nassistant:"
        raw = None
        for _attempt in range(2):   # one retry on provider timeout/flake
            try:
                raw, _provider = ctx.llm.generate(
                    _system_prompt(), prompt, sensitive=False)
                break
            except Exception:
                continue
        if raw is None:
            return {"reply": "The LLM provider timed out — please try again "
                             "in a moment.", "steps": steps}
        step = _parse_step(raw)

        if "final" in step:
            return {"reply": str(step["final"]), "steps": steps}

        tool = str(step.get("tool") or "")
        args = step.get("args") or {}
        try:
            result = _call_tool(ctx, tool, args)
            ok = True
        except Exception as exc:
            result = {"error": str(exc)}
            ok = False
        steps.append({"tool": tool, "args": args, "ok": ok, "result": result})
        transcript.append(f"assistant: {json.dumps({'tool': tool, 'args': args})}")
        transcript.append(
            "tool_result: " + json.dumps(result, default=str)[:3000])

    return {"reply": "I hit the tool-step limit before finishing — "
                     "try a more specific question.", "steps": steps}
