"""Agent routes — audit export (R7A-6) and, from R4, the orchestrator goal API.

Route-order note: all paths here are exact (no parameterized segments yet);
if any are added later, keep exact paths first.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..db import User
from ..deps import current_user
from .automation import _ctx

router = APIRouter()


class GoalBody(BaseModel):
    goal: str


def _sweep_stale_runs(ctx, minutes: int = 15) -> None:
    """Runs stuck 'running' past the budget window (server restarted mid-run,
    thread died) are marked interrupted. Called from BOTH the POST and the
    list GET — the frontend polls the list, so a zombie row would otherwise
    keep it polling forever."""
    import datetime as _dt
    stale_cut = (_dt.datetime.now(_dt.timezone.utc)
                 - _dt.timedelta(minutes=minutes)).isoformat()
    ctx.collab.conn.execute(
        "UPDATE agent_goals SET status='failed',"
        " summary='interrupted (server restart or time budget exceeded)'"
        " WHERE status='running' AND ts<?", (stale_cut,))
    ctx.collab.conn.commit()


@router.post("/api/agent/goal")
def agent_goal(body: GoalBody, user: User = Depends(current_user)):
    """Orchestrator: natural-language goal → plan → tool calls (reads direct,
    writes parked in the Approval Inbox) → summary + plan graph.

    Runs in a BACKGROUND thread — a run is up to ~12 sequential LLM calls
    and blocked the request for minutes. Returns {run_id, status:"running"}
    immediately; poll GET /api/agent/goals until that run leaves 'running'.
    One run at a time: a second POST while one is active is refused."""
    import datetime as _dt
    import threading
    import uuid as _uuid
    from ...automation.orchestrator import run_goal, _ensure_table

    goal = (body.goal or "").strip()
    if not goal:
        raise HTTPException(status_code=400, detail="goal is required")

    cdb, ctx = _ctx(user, with_llm=False)
    try:
        _ensure_table(ctx)
        _sweep_stale_runs(ctx)
        active = ctx.collab.conn.execute(
            "SELECT id FROM agent_goals WHERE status='running' LIMIT 1").fetchone()
        if active:
            raise HTTPException(status_code=409,
                                detail="A goal run is already in progress — wait for it to finish.")
        run_id = _uuid.uuid4().hex[:12]
        ctx.collab.conn.execute(
            "INSERT INTO agent_goals(id,ts,goal,plan,steps,summary,status)"
            " VALUES(?,?,?,?,?,?,?)",
            (run_id, _dt.datetime.now(_dt.timezone.utc).isoformat(), goal,
             "[]", "[]", "", "running"))
        ctx.collab.conn.commit()
    finally:
        cdb.close()

    def _worker():
        try:
            cdb2, ctx2 = _ctx(user)
            try:
                run_goal(ctx2, goal, run_id=run_id)
            finally:
                cdb2.close()
        except Exception as exc:   # run_goal persists its own failures; this
            try:                    # catches crashes outside it (ctx build etc.)
                cdb3, ctx3 = _ctx(user, with_llm=False)
                try:
                    ctx3.collab.conn.execute(
                        "UPDATE agent_goals SET status='failed', summary=? WHERE id=?",
                        (f"crashed: {exc}", run_id))
                    ctx3.collab.conn.commit()
                finally:
                    cdb3.close()
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True, name=f"agent-goal-{run_id}").start()
    return {"run_id": run_id, "status": "running"}


@router.get("/api/agent/goals")
def agent_goal_runs(limit: int = 20, user: User = Depends(current_user)):
    from ...automation.orchestrator import list_goal_runs, _ensure_table
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        _ensure_table(ctx)
        _sweep_stale_runs(ctx)
        return {"runs": list_goal_runs(ctx, limit=limit)}
    finally:
        cdb.close()


@router.get("/api/agent/audit")
def agent_audit(from_: str | None = Query(None, alias="from"),
                to: str | None = Query(None, alias="to"),
                user: User = Depends(current_user)):
    """Structured regulator-style report: agent actions with reasoning,
    approvals/rejections, run ledger, decision journal, screening flags,
    plus LLM-routing documentation in the metadata."""
    from ...automation.audit import build_audit_report
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        return build_audit_report(ctx, since=from_, until=to)
    finally:
        cdb.close()
