"""Career routes (CAREER AUTOPILOT Part 6) — profile, postings,
applications, portfolio, and the "apply" action. Every route reuses the
existing career data model/logic (Parts 1-5) — no parallel storage.

GET /api/career/portfolio has side effects (it IS the "on-demand/manual
button" trigger for portfolio_analyze — Part 3's plan explicitly deferred
that trigger to this route): it can propose a gap-project batch approval
and always writes a vault note. Slower than a typical GET (one LLM call)
but idempotent per day via portfolio_analyze's own dedup/note-eid, so
repeat clicks are harmless.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import User
from ..deps import current_user
from .automation import _ctx

router = APIRouter()


class CareerProfileBody(BaseModel):
    target_role: str | None = None
    target_location: str | None = None
    remote_ok: bool | None = None
    deadline: str | None = None
    resume_text: str | None = None
    skills: list[str] | None = None


class ApplicationStatusBody(BaseModel):
    status: str
    note: str = ""


def _active_career_goal(ctx) -> dict | None:
    row = ctx.collab.conn.execute(
        "SELECT * FROM goals WHERE domain='career' AND status='active'"
        " ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        from ...autonomous import GoalEngine
        d["computed_progress"] = GoalEngine(ctx.collab).progress(d["id"])
    except Exception:
        d["computed_progress"] = d.get("progress") or 0.0
    return d


@router.get("/api/career/profile")
def get_career_profile(user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        profile = ctx.store.get_career_profile(user.id) or {}
        profile.pop("resume_text", None)   # never return raw resume text over the wire
        return {"profile": profile, "goal": _active_career_goal(ctx),
               "funnel": ctx.store.career_funnel_counts(user.id)}
    finally:
        cdb.close()


@router.put("/api/career/profile")
def put_career_profile(body: CareerProfileBody, user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        ctx.store.set_career_profile(
            user.id, target_role=body.target_role, target_location=body.target_location,
            remote_ok=body.remote_ok, deadline=body.deadline,
            resume_text=body.resume_text, skills=body.skills)
        return {"ok": True}
    finally:
        cdb.close()


@router.get("/api/career/postings")
def list_career_postings(status: str | None = None, limit: int = 50,
                         user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        return {"postings": ctx.store.list_postings(user.id, status=status, limit=limit)}
    finally:
        cdb.close()


@router.get("/api/career/applications")
def list_career_applications(status: str | None = None, user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        return {"applications": ctx.store.list_applications(user.id, status=status),
               "funnel": ctx.store.career_funnel_counts(user.id)}
    finally:
        cdb.close()


@router.patch("/api/career/applications/{application_id}")
def update_career_application(application_id: str, body: ApplicationStatusBody,
                              user: User = Depends(current_user)):
    """Human-reported outcome (got a response/interview/offer, or a
    rejection) — not an agent proposal, so this writes directly rather
    than going through the approval queue: the user is telling Amy what
    already happened in the real world, not asking Amy to do something."""
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        try:
            ok = ctx.store.update_application_status(
                user.id, application_id, body.status, body.note)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not ok:
            raise HTTPException(status_code=404, detail="application not found")
        try:
            from ...events.store import CAREER_APPLICATION_STATUS_CHANGED
            ctx.events().emit(CAREER_APPLICATION_STATUS_CHANGED,
                              {"application_id": application_id, "status": body.status},
                              source="career_ui")
        except Exception:
            pass
        return {"ok": True}
    finally:
        cdb.close()


@router.get("/api/career/portfolio")
def get_career_portfolio(user: User = Depends(current_user)):
    cdb, ctx = _ctx(user)
    try:
        from ...agents.reactive import portfolio_analyze
        return portfolio_analyze(ctx.events(), ctx)
    finally:
        cdb.close()


@router.post("/api/career/postings/{posting_id}/apply")
def apply_to_career_posting(posting_id: str, force: bool = False,
                            user: User = Depends(current_user)):
    """PREPARE + one approval (Part 5) — never sends anything itself; the
    approval still requires an explicit approve in the Approval Inbox.

    Part 5E duplicate guard: a company with an active (or recently
    rejected/ghosted) application 409s with the reason; the human can
    override with ?force=true. The agent path has no override — for
    job_scout's auto-proposals the guard is absolute."""
    cdb, ctx = _ctx(user)
    try:
        from ...career_apply import prepare_application
        result = prepare_application(ctx, posting_id, force=force)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        if "blocked" in result:
            raise HTTPException(
                status_code=409,
                detail=f"{result['blocked']} — re-apply anyway with "
                       f"?force=true if this is intentional.")
        return result
    finally:
        cdb.close()
