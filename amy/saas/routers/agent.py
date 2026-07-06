"""Agent routes — audit export (R7A-6) and, from R4, the orchestrator goal API.

Route-order note: all paths here are exact (no parameterized segments yet);
if any are added later, keep exact paths first.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..db import User
from ..deps import current_user
from .automation import _ctx

router = APIRouter()


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
