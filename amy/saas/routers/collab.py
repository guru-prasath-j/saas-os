"""Collaboration: multi-agent ask/stream, planner goals/milestones, reflect, learn, memory."""
from __future__ import annotations

import json as _json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..db import User
from .. import paths, security
from ..deps import (
    current_user, Query,
    _engine_for, _user_key, _collab_db_path, _collab_light, _journal_user,
)

router = APIRouter()


def _user_mcp_connectors(user: User) -> list[dict]:
    """Decrypt this user's registered MCP connectors into plain dicts for
    CollabMaster's live-context injection (amy/collab/orchestrator.py
    _career_context()) — decrypted here, not in amy/collab/, so that package
    doesn't need to import amy.saas.db/security."""
    from ..db import McpConnector, SessionLocal
    db = SessionLocal()
    try:
        rows = db.query(McpConnector).filter(McpConnector.user_id == user.id).all()
        out = []
        for r in rows:
            try:
                auth_value = security.decrypt_secret(r.auth_ref) if r.auth_ref else None
            except Exception:
                auth_value = None
            out.append({"name": r.name, "server_url": r.server_url, "auth_type": r.auth_type,
                       "auth_value": auth_value, "auth_extra": r.auth_extra})
        return out
    finally:
        db.close()


class Goal(BaseModel):
    title: str
    domain: str = "general"
    target_date: str | None = None


class Milestone(BaseModel):
    title: str


class Pref(BaseModel):
    key: str
    value: str


class FinanceTarget(BaseModel):
    savings_target: float
    monthly_savings_category: str = "Savings"


def _sse(obj) -> str:
    return f"data: {_json.dumps(obj)}\n\n"


@router.post("/api/collab/ask/stream")
def collab_ask_stream(q: Query, user: User = Depends(current_user)):
    from ...collab import CollabMaster
    from ...llm import LLMRouter
    eng = _engine_for(user)
    key = _user_key(user)
    db_path = _collab_db_path(user)
    notes = eng.notes

    def gen():
        yield _sse({"type": "status", "data": "thinking"})
        vault = str(paths.vault_dir(user.id))
        cm = CollabMaster(notes, db_path,
                          llm=LLMRouter(openai_api_key=key, use_global_keys=False),
                          vault_path=vault,
                          finance_db_path=str(paths.index_dir(user.id) / "finance.db"),
                          connector_dir=str(paths.index_dir(user.id) / "connectors"),
                          mcp_connectors=_user_mcp_connectors(user))
        try:
            res = cm.handle(q.text)
            yield _sse({"type": "done", "data": res})
        except Exception as e:
            yield _sse({"type": "error", "data": str(e)})
        finally:
            cm.close()
            try:
                _journal_user(user)
            except Exception:
                pass

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/api/collab/ask")
def collab_ask(q: Query, user: User = Depends(current_user)):
    from ...collab import CollabMaster
    from ...llm import LLMRouter
    eng = _engine_for(user)
    cm = CollabMaster(eng.notes, _collab_db_path(user),
                      llm=LLMRouter(openai_api_key=_user_key(user), use_global_keys=False),
                      vault_path=str(paths.vault_dir(user.id)),
                      finance_db_path=str(paths.index_dir(user.id) / "finance.db"),
                      connector_dir=str(paths.index_dir(user.id) / "connectors"),
                      mcp_connectors=_user_mcp_connectors(user))
    try:
        result = cm.handle(q.text)
        _journal_user(user)
        return result
    finally:
        cm.close()


@router.post("/api/goals")
def create_goal(body: Goal, user: User = Depends(current_user)):
    db, _, planner, *_ = _collab_light(user)
    try:
        return {"id": planner.create_goal(body.title, body.domain, body.target_date)}
    finally:
        db.close()


@router.get("/api/goals")
def list_goals(user: User = Depends(current_user)):
    db, _, planner, *_ = _collab_light(user)
    try:
        return {"goals": planner.list_goals()}
    finally:
        db.close()


@router.post("/api/goals/{goal_id}/milestones")
def add_milestone(goal_id: str, body: Milestone, user: User = Depends(current_user)):
    db, _, planner, *_ = _collab_light(user)
    try:
        return {"id": planner.add_milestone(goal_id, body.title),
                "plan": planner.get_plan(goal_id)}
    finally:
        db.close()


@router.post("/api/milestones/{milestone_id}/complete")
def complete_milestone(milestone_id: str, done: bool = True,
                       user: User = Depends(current_user)):
    db, _, planner, *_ = _collab_light(user)
    try:
        planner.complete_milestone(milestone_id, done)
        return {"ok": True}
    finally:
        db.close()


@router.post("/api/goals/{goal_id}/finance-target")
def set_goal_finance_target(goal_id: str, body: FinanceTarget,
                             user: User = Depends(current_user)):
    """Link a savings target to a goal for drift tracking."""
    db, _, planner, *_ = _collab_light(user)
    try:
        ok = planner.set_finance_target(
            goal_id, body.savings_target, body.monthly_savings_category)
        if not ok:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="goal not found")
        return {"ok": True, "goal_id": goal_id,
                "savings_target": body.savings_target,
                "monthly_savings_category": body.monthly_savings_category}
    finally:
        db.close()


@router.get("/api/finance/drift")
def goal_drift_report(user: User = Depends(current_user)):
    """
    Return drift analysis for all finance-linked active goals.
    high_drift=True means > 30% behind required savings rate.
    """
    from ...autonomous.executive import ExecutiveAgent
    from .. import paths
    db, *_ = _collab_light(user)
    try:
        agent = ExecutiveAgent(
            db,
            finance_db_path=str(paths.index_dir(user.id) / "finance.db"),
        )
        return {"drift_reports": agent.analyze_finance_drift()}
    finally:
        db.close()


@router.get("/api/reflect")
def reflect(days: int = 7, user: User = Depends(current_user)):
    db, _, _, reflection, _ = _collab_light(user)
    try:
        return reflection.weekly_summary(days)
    finally:
        db.close()


@router.get("/api/learn")
def learn(window_days: int = 7, user: User = Depends(current_user)):
    db, _, _, _, learning = _collab_light(user)
    try:
        return {"trends": learning.trends(window_days),
                "recommendations": learning.recommendations(window_days)}
    finally:
        db.close()


@router.get("/api/memory")
def memory_snapshot(user: User = Depends(current_user)):
    db, mem, *_ = _collab_light(user)
    try:
        return mem.snapshot()
    finally:
        db.close()


@router.post("/api/memory/pref")
def set_pref(body: Pref, user: User = Depends(current_user)):
    db, mem, *_ = _collab_light(user)
    try:
        mem.set_pref(body.key, body.value)
        return {"ok": True}
    finally:
        db.close()
