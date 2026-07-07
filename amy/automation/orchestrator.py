"""Orchestrator agent (Phase R4) — natural-language goal → plan → gated tools.

Grown from the assistant's loop (same one-JSON-object protocol, same
provider-retry), with three upgrades:
  1. an explicit PLAN produced first and persisted,
  2. every tool call runs with actor="agent", so the R3 approval gate parks
     anything write/destructive — the orchestrator can *propose* freely but
     never act on data without the human,
  3. plan → steps → outcomes stored as GraphStore nodes/edges and the run
     journaled to the vault.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import uuid
from pathlib import Path

import re

from .assistant import _catalog
from .executors import JobCtx

_MAX_TOOL_CALLS = 10
_PLAN_MAX_STEPS = 4     # was 6 — with a slow thinking-model each extra step
                        # is 1-2 more long LLM calls; 4 keeps runs bounded
_TIME_BUDGET_S = 300    # wall clock: past this, remaining steps are skipped
                        # and the run summarizes what it did get done


def _first_obj(raw: str) -> dict | None:
    """First complete JSON object in the response — like the assistant's
    _parse_step but without its tool/final key filter (plans and summaries
    are arbitrary objects)."""
    raw = re.sub(r"```(?:json)?", "", raw or "").strip()
    decoder = json.JSONDecoder()
    idx = raw.find("{")
    while idx != -1:
        try:
            obj, _end = decoder.raw_decode(raw, idx)
        except Exception:
            idx = raw.find("{", idx + 1)
            continue
        if isinstance(obj, dict):
            return obj
        idx = raw.find("{", idx + 1)
    return None


# ---------------------------------------------------------------------------
# Storage (agent_goals table in collab.db)
# ---------------------------------------------------------------------------

def _ensure_table(ctx: JobCtx):
    ctx.collab.conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_goals ("
        " id TEXT PRIMARY KEY, ts TEXT, goal TEXT, plan TEXT,"
        " steps TEXT, summary TEXT, status TEXT)")
    ctx.collab.conn.commit()


def list_goal_runs(ctx: JobCtx, limit: int = 20) -> list[dict]:
    _ensure_table(ctx)
    rows = ctx.collab.conn.execute(
        "SELECT * FROM agent_goals ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["plan"] = json.loads(d["plan"] or "[]")
        d["steps"] = json.loads(d["steps"] or "[]")
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# LLM plumbing
# ---------------------------------------------------------------------------

def _gen(ctx: JobCtx, system: str, prompt: str) -> dict | None:
    for _ in range(2):   # one retry on provider flake
        try:
            # fast=True: each call here is a one-JSON-object decision, not
            # analysis — thinking mode made these 46s median (measured)
            raw, _p = ctx.llm.generate(system, prompt, sensitive=False, fast=True)
            return _first_obj(raw)
        except Exception:
            continue
    return None


def _context_block(ctx: JobCtx) -> str:
    """Situational awareness via ContextModule over recent persisted events."""
    try:
        from ..context import ContextModule
        cm = ContextModule(ctx.events())
        for ev in reversed(ctx.events().recent(n=30)):
            cm._on_event(ev)
        return cm.get_context(15)
    except Exception:
        return "No recent activity."


# ---------------------------------------------------------------------------
# Graph persistence
# ---------------------------------------------------------------------------

def _store_plan_graph(ctx: JobCtx, run_id: str, goal: str,
                      plan: list[str]) -> list[str]:
    """goal node + one task node per step; belongs_to + depends_on edges.
    Returns task node ids (indexed by step)."""
    from ..knowledge_graph.store import GraphStore
    g = GraphStore(str(Path(ctx.finance_path).parent / "graph.db"))
    try:
        goal_node = f"agentgoal:{run_id}"
        g.add_node(goal_node, "goal", goal[:120], ref=f"agent_goals/{run_id}")
        task_ids = []
        for i, step in enumerate(plan):
            tid = f"agenttask:{run_id}:{i}"
            g.add_node(tid, "task", step[:120], ref="planned")
            g.add_edge(tid, goal_node, "belongs_to")
            if task_ids:
                g.add_edge(tid, task_ids[-1], "depends_on")
            task_ids.append(tid)
        g.commit()
        return task_ids
    finally:
        g.conn.close()


def _mark_task(ctx: JobCtx, task_id: str, label: str, outcome: str):
    from ..knowledge_graph.store import GraphStore
    g = GraphStore(str(Path(ctx.finance_path).parent / "graph.db"))
    try:
        g.add_node(task_id, "task", label[:120], ref=outcome[:400])
        g.commit()
    finally:
        g.conn.close()


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------

_PLAN_SYSTEM = (
    "You are Amy's orchestrator. Turn the user's goal into a short concrete "
    "plan using the available tools.\n\nTools:\n{catalog}\n\n"
    "Respond with EXACTLY ONE JSON object:\n"
    '  {{"plan": ["step 1", "step 2", ...], "reasoning": "why this plan"}}\n'
    f"Max {_PLAN_MAX_STEPS} steps. Steps must be achievable with the tools "
    "(reads for analysis, writes become approval requests for the user)."
)

_STEP_SYSTEM = (
    "You are Amy's orchestrator executing a plan step by step. "
    "Tools marked [write]/[destructive] are PARKED for the user's approval "
    "when you call them — that still counts as completing the step "
    "(proposing is your job; the human decides).\n\nTools:\n{catalog}\n\n"
    "Respond with EXACTLY ONE JSON object, one of:\n"
    '  {{"tool": "<name>", "args": {{...}}, "reasoning": "why this call"}}\n'
    '  {{"step_done": "<what this step concluded>"}}\n'
    "Never invent data — read it with tools first."
)

_SUMMARY_SYSTEM = (
    "Summarize this orchestrator run for the user in 2-4 sentences: what was "
    "analyzed, what was found, and what is now waiting for their approval. "
    'Respond with EXACTLY ONE JSON object: {"summary": "..."}'
)


def _persist_run(ctx: JobCtx, run_id: str, goal: str, plan: list,
                 steps: list, summary: str, status: str):
    ctx.collab.conn.execute(
        "INSERT OR REPLACE INTO agent_goals(id,ts,goal,plan,steps,summary,status)"
        " VALUES(?,?,?,?,?,?,?)",
        (run_id, _dt.datetime.now(_dt.timezone.utc).isoformat(), goal,
         json.dumps(plan), json.dumps(steps, default=str), summary, status))
    ctx.collab.conn.commit()


def run_goal(ctx: JobCtx, goal: str, max_tool_calls: int = _MAX_TOOL_CALLS,
             run_id: str | None = None) -> dict:
    """When run_id is given (background mode), the caller pre-inserted a
    status='running' row — every exit path below replaces it, so a poller
    always sees the run finish (completed / failed)."""
    _ensure_table(ctx)
    run_id = run_id or uuid.uuid4().hex[:12]
    if ctx.llm is None:
        _persist_run(ctx, run_id, goal, [], [],
                     "No LLM provider is available right now.", "failed")
        return {"error": "No LLM provider is available right now."}
    catalog = _catalog()
    context = _context_block(ctx)

    # --- 1. plan -------------------------------------------------------------
    plan_resp = _gen(ctx, _PLAN_SYSTEM.format(catalog=catalog),
                     f"Recent activity:\n{context}\n\nGoal: {goal}")
    if not plan_resp or not isinstance(plan_resp.get("plan"), list) \
            or not plan_resp["plan"]:
        _persist_run(ctx, run_id, goal, [], [],
                     "Could not produce a plan — try rephrasing the goal.", "failed")
        return {"error": "Could not produce a plan — try rephrasing the goal."}
    plan = [str(s) for s in plan_resp["plan"][:_PLAN_MAX_STEPS]]
    plan_reasoning = str(plan_resp.get("reasoning") or "")
    task_ids = _store_plan_graph(ctx, run_id, goal, plan)

    # --- 2. execute ------------------------------------------------------------
    import time as _time
    from .. import tools
    deadline = _time.monotonic() + _TIME_BUDGET_S
    steps_log: list[dict] = []
    calls_used = 0
    for i, step in enumerate(plan):
        if _time.monotonic() > deadline:
            if i < len(task_ids):
                _mark_task(ctx, task_ids[i], step, "skipped (time budget)")
            continue
        step_outcome = "skipped (tool budget exhausted)"
        transcript = [f"Goal: {goal}", f"Plan: {json.dumps(plan)}",
                      f"Current step ({i + 1}/{len(plan)}): {step}"]
        for log in steps_log[-4:]:
            transcript.append(f"Earlier: {json.dumps(log, default=str)[:400]}")
        while calls_used < max_tool_calls and _time.monotonic() <= deadline:
            resp = _gen(ctx, _STEP_SYSTEM.format(catalog=catalog),
                        "\n".join(transcript) + "\nassistant:")
            if resp is None:
                step_outcome = "LLM unavailable"
                break
            if "step_done" in resp or "final" in resp:
                step_outcome = str(resp.get("step_done") or resp.get("final"))
                break
            tool_name = str(resp.get("tool") or "")
            args = resp.get("args") or {}
            reasoning = str(resp.get("reasoning") or f"step {i + 1}: {step}")
            ctx._extras["agent_name"] = "orchestrator"
            ctx._extras["agent_reasoning"] = reasoning
            # Found via manual testing: running an equivalent goal twice
            # ("cut spending 10%" vs "reduce spending by 10 percent") queued
            # two separate approvals for the IDENTICAL action. Dedup by
            # tool+args (not by goal phrasing) so a repeat proposal for the
            # same underlying change collapses into the existing pending
            # one; a fresh proposal is still allowed after rejection, since
            # create_approval's dedup only blocks pending/executed rows.
            ctx._extras["agent_dedup_key"] = (
                "orch_" + tool_name + "_" + hashlib.sha256(
                    json.dumps(args, sort_keys=True, default=str).encode()
                ).hexdigest()[:16])
            try:
                result = tools.invoke(ctx, tool_name, args, actor="agent")
                ok = True
            except Exception as exc:
                result = {"error": str(exc)}
                ok = False
            calls_used += 1
            entry = {"step": i, "tool": tool_name, "args": args,
                     "reasoning": reasoning, "ok": ok,
                     "queued": isinstance(result, dict) and result.get("status") == "pending",
                     "result": result}
            steps_log.append(entry)
            transcript.append(f"assistant: {json.dumps({'tool': tool_name, 'args': args})}")
            transcript.append("tool_result: " + json.dumps(result, default=str)[:2000])
        if i < len(task_ids):
            _mark_task(ctx, task_ids[i], step, step_outcome)

    # --- 3. summarize + persist + journal ---------------------------------------
    sum_resp = _gen(ctx, _SUMMARY_SYSTEM,
                    f"Goal: {goal}\nPlan: {json.dumps(plan)}\n"
                    f"Steps: {json.dumps(steps_log, default=str)[:4000]}")
    summary = str((sum_resp or {}).get("summary") or
                  f"Ran {len(plan)} step(s), {calls_used} tool call(s).")
    queued = sum(1 for s in steps_log if s.get("queued"))
    status = "completed" if calls_used or steps_log else "planned_only"
    _persist_run(ctx, run_id, goal, plan, steps_log, summary, status)

    try:
        payload = {"agent": "orchestrator", "summary": f"Goal run: {goal[:80]}",
                   "reasoning": plan_reasoning or summary, "run_id": run_id,
                   "plan": plan, "queued_approvals": queued}
        eid = ctx.events().emit("agent.goal_planned", payload, source="orchestrator")
        from ..agents.reactive import _journal
        _journal(ctx, {"id": eid, "type": "agent.goal_planned",
                       "payload": payload, "ts": None, "source": "orchestrator"})
    except Exception:
        pass   # fire-and-forget: the run row above is already the record

    return {"run_id": run_id, "goal": goal, "plan": plan,
            "plan_reasoning": plan_reasoning, "steps": steps_log,
            "summary": summary, "queued_approvals": queued, "status": status}
