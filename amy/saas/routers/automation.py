"""Automation layer routes — jobs, run ledger, Approval Inbox, LLM health,
event dead letters, and the AI assistant chat console.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import User
from ..deps import current_user, _collab_db_path, _user_key
from .. import paths

router = APIRouter()


# --- schemas -----------------------------------------------------------------

class JobPatch(BaseModel):
    enabled: bool | None = None
    schedule: dict | None = None


class RejectBody(BaseModel):
    reason: str = ""


class ChatBody(BaseModel):
    message: str
    history: list[dict] = []


# --- helpers -----------------------------------------------------------------

def _ctx(user: "User", with_llm: bool = True):
    """Returns (collab_db, JobCtx). Caller must close the collab_db."""
    from ...automation import build_ctx
    from ...collab import CollabDB
    cdb = CollabDB(_collab_db_path(user))
    llm_router = None
    if with_llm:
        try:
            from ...llm import LLMRouter
            llm_router = LLMRouter(openai_api_key=_user_key(user),
                                   use_global_keys=True)
        except Exception:
            llm_router = None
    home = (getattr(user, "home_jurisdiction", None) or "india").lower()
    active = [j.strip().lower()
              for j in (getattr(user, "active_jurisdictions", None) or "").split(",")
              if j.strip()]
    ctx = build_ctx(user.id, user.email, cdb, paths.index_dir(user.id),
                    llm_router=llm_router,
                    jurisdictions=list(dict.fromkeys([home] + active)),
                    language=getattr(user, "language", None))
    return cdb, ctx


# --- jobs ---------------------------------------------------------------------

@router.get("/api/automation/status")
def automation_status(user: User = Depends(current_user)):
    from ...automation import ensure_defaults
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        ensure_defaults(ctx.store)
        return {
            "paused": ctx.store.paused(),
            "jobs": ctx.store.list_jobs(),
            "pending_approvals": ctx.store.pending_count(),
        }
    finally:
        cdb.close()


@router.post("/api/automation/pause")
def automation_pause(user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        ctx.store.set_paused(True)
        return {"paused": True}
    finally:
        cdb.close()


@router.post("/api/automation/resume")
def automation_resume(user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        ctx.store.set_paused(False)
        return {"paused": False}
    finally:
        cdb.close()


@router.get("/api/automation/jobs")
def list_jobs(user: User = Depends(current_user)):
    from ...automation import ensure_defaults
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        ensure_defaults(ctx.store)
        return {"jobs": ctx.store.list_jobs()}
    finally:
        cdb.close()


@router.patch("/api/automation/jobs/{name}")
def patch_job(name: str, body: JobPatch, user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        if not ctx.store.update_job(name, enabled=body.enabled,
                                    schedule=body.schedule):
            raise HTTPException(status_code=404, detail="job not found")
        return {"job": ctx.store.get_job(name)}
    finally:
        cdb.close()


@router.post("/api/automation/jobs/{name}/run")
def run_job_now(name: str, user: User = Depends(current_user)):
    from ...automation import run_job, HANDLERS
    if name not in HANDLERS:
        raise HTTPException(status_code=404, detail="unknown job")
    cdb, ctx = _ctx(user)
    try:
        return run_job(ctx, name)
    finally:
        cdb.close()


@router.get("/api/automation/runs")
def list_runs(job: str | None = None, limit: int = 50,
              user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        return {"runs": ctx.store.list_runs(job_name=job, limit=limit)}
    finally:
        cdb.close()


# --- approval inbox -------------------------------------------------------------

@router.get("/api/automation/approvals")
def list_approvals(status: str | None = "pending", limit: int = 100,
                   user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        return {"approvals": ctx.store.list_approvals(
            status=None if status in (None, "", "all") else status, limit=limit)}
    finally:
        cdb.close()


@router.post("/api/automation/approvals/{aid}/approve")
def approve_action(aid: str, user: User = Depends(current_user)):
    from ...automation import approve
    cdb, ctx = _ctx(user)
    try:
        return approve(ctx, aid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cdb.close()


@router.post("/api/automation/approvals/{aid}/reject")
def reject_action(aid: str, body: RejectBody = RejectBody(),
                  user: User = Depends(current_user)):
    from ...automation import reject
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        return reject(ctx, aid, body.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cdb.close()


# --- observability --------------------------------------------------------------

@router.get("/api/automation/llm-stats")
def llm_stats(hours: int = 168, user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        return ctx.store.llm_stats(hours=hours)
    finally:
        cdb.close()


@router.get("/api/automation/dead-letters")
def dead_letters(limit: int = 50, user: User = Depends(current_user)):
    from ...events.store import EventStore
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        return {"dead_letters": EventStore(cdb).dead_letters(limit)}
    finally:
        cdb.close()


# --- learned categorizer rules ---------------------------------------------------

@router.get("/api/automation/learned-rules")
def learned_rules(user: User = Depends(current_user)):
    from ...automation import learning
    from ...finance.engine import FinanceEngine
    fe = FinanceEngine(str(paths.index_dir(user.id) / "finance.db"))
    try:
        return {"rules": learning.list_rules(fe)}
    finally:
        fe.close()


# --- AI assistant ----------------------------------------------------------------

@router.post("/api/assistant/chat")
def assistant_chat(body: ChatBody, user: User = Depends(current_user)):
    from ...automation import assistant
    cdb, ctx = _ctx(user)
    try:
        return assistant.chat(ctx, body.message, history=body.history)
    finally:
        cdb.close()
