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


@router.post("/api/agent/goal")
def agent_goal(body: GoalBody, user: User = Depends(current_user)):
    """Orchestrator: natural-language goal → plan → tool calls (reads direct,
    writes parked in the Approval Inbox) → summary + plan graph."""
    from ...automation.orchestrator import run_goal
    goal = (body.goal or "").strip()
    if not goal:
        raise HTTPException(status_code=400, detail="goal is required")
    cdb, ctx = _ctx(user)
    try:
        return run_goal(ctx, goal)
    finally:
        cdb.close()


@router.get("/api/agent/goals")
def agent_goal_runs(limit: int = 20, user: User = Depends(current_user)):
    from ...automation.orchestrator import list_goal_runs
    cdb, ctx = _ctx(user, with_llm=False)
    try:
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
