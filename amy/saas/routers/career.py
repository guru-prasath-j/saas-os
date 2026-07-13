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
    resume_version_id: str | None = None   # Phase D — optional, attach after the fact


class PortfolioItemRefreshBody(BaseModel):
    repo_name: str


class ResumeVersionBody(BaseModel):
    target_track: str
    label: str | None = None


class InterviewLogBody(BaseModel):
    application_id: str | None = None
    company: str = ""
    round_type: str = "other"
    questions: list[str] | None = None
    self_assessed_outcome: str = "ok"
    weakness_tags: list[str] | None = None
    notes: str = ""


class CompanyTargetBody(BaseModel):
    is_target: bool


class CareerLadderBody(BaseModel):
    """Part 5F: edit the active goal's ladder — target_role is what gets
    scouted/applied for NOW; north_star_role (optional, "" clears it) is
    the longer-term destination that learning/portfolio build toward."""
    target_role: str | None = None
    north_star_role: str | None = None


def _active_career_goal(ctx) -> dict | None:
    import json as _json
    row = ctx.collab.conn.execute(
        "SELECT * FROM goals WHERE domain='career' AND status='active'"
        " ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        meta = _json.loads(d.get("career_meta") or "{}")
    except Exception:
        meta = {}
    # parsed ladder fields for the frontend (career_meta stays the raw JSON)
    d["target_role"] = meta.get("target_role")
    d["north_star_role"] = meta.get("north_star_role")
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


@router.patch("/api/career/goal")
def update_career_ladder(body: CareerLadderBody, user: User = Depends(current_user)):
    """Part 5F: edit the active career goal's ladder roles in place. This
    is THE way to change what gets scouted — the scout reads the goal's
    career_meta.target_role first (editing the profile's role alone does
    NOT re-aim scouting, a UX trap found live). target_role changes are
    mirrored to the profile so ATS/drafts follow. Direct human edit of the
    user's own stated intent — not gated, same stance as the profile PUT."""
    import json as _json
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        row = ctx.collab.conn.execute(
            "SELECT id, career_meta FROM goals WHERE domain='career' AND"
            " status='active' ORDER BY created_at DESC LIMIT 1").fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="no active career goal")
        try:
            meta = _json.loads(row["career_meta"] or "{}")
        except Exception:
            meta = {}
        if body.target_role is not None and body.target_role.strip():
            meta["target_role"] = body.target_role.strip()[:120]
            ctx.store.set_career_profile(user.id, target_role=meta["target_role"])
        if body.north_star_role is not None:
            ns = body.north_star_role.strip()[:120]
            if ns and ns.lower() != (meta.get("target_role") or "").lower():
                meta["north_star_role"] = ns
            else:
                meta.pop("north_star_role", None)   # "" (or same role) clears it
        ctx.collab.conn.execute("UPDATE goals SET career_meta=? WHERE id=?",
                                (_json.dumps(meta), row["id"]))
        ctx.collab.conn.commit()
        return {"ok": True, "target_role": meta.get("target_role"),
               "north_star_role": meta.get("north_star_role")}
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
        if body.resume_version_id:
            ctx.store.set_application_resume_version(
                user.id, application_id, body.resume_version_id)
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


@router.get("/api/career/skill-demand")
def get_career_skill_demand(track: str | None = None, propose: bool = True,
                            user: User = Depends(current_user)):
    """CAREER AUTOPILOT Phase A — market-demand report over job_postings.
    keywords. Has side effects like GET /api/career/portfolio below (may
    propose Learning Feed focuses for frequently-demanded missing
    skills, tier-2 by default) — same established precedent in this
    router, set propose=false to compute without proposing."""
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        from ...career_scout import skill_demand_report, skill_demand_reports
        if track:
            return skill_demand_report(ctx, track, propose=propose)
        return {"tracks": skill_demand_reports(ctx, propose=propose)}
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


@router.get("/api/career/sprint/current")
def get_current_sprint(user: User = Depends(current_user)):
    """CAREER AUTOPILOT Phase C: the most-recently generated weekly
    sprint goal (domain='career_sprint' — deliberately NOT 'career', see
    amy/career_sprint.py's module docstring), with milestones/tasks +
    computed progress — same shape _active_career_goal above returns for
    the main career goal."""
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        from ...career_sprint import _latest_sprint_goal
        from ...autonomous import GoalEngine
        sprint = _latest_sprint_goal(ctx)
        if sprint is None:
            return {"sprint": None}
        engine = GoalEngine(ctx.collab)
        milestones = [dict(r) for r in ctx.collab.conn.execute(
            "SELECT id,title,done FROM milestones WHERE goal_id=?",
            (sprint["id"],)).fetchall()]
        return {"sprint": {
            **sprint,
            "milestones": milestones,
            "tasks": engine.list_tasks(sprint["id"]),
            "progress": engine.progress(sprint["id"]),
        }}
    finally:
        cdb.close()


@router.get("/api/career/sprint/history")
def get_sprint_history(limit: int = 12, user: User = Depends(current_user)):
    """Past sprint goals (target_date already passed), most recent first."""
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        import datetime as _dt
        today = _dt.date.today().isoformat()
        rows = ctx.collab.conn.execute(
            "SELECT * FROM goals WHERE domain='career_sprint'"
            " AND target_date IS NOT NULL AND target_date < ? ORDER BY created_at DESC"
            " LIMIT ?", (today, limit)).fetchall()
        return {"sprints": [dict(r) for r in rows]}
    finally:
        cdb.close()


@router.get("/api/career/portfolio/items")
def list_portfolio_items(classification: str | None = None,
                         user: User = Depends(current_user)):
    """CAREER AUTOPILOT Phase D: persisted SHOWCASE/NEEDS_WORK/NOT_RELEVANT
    classification (amy/career_portfolio.py) — previously only reached a
    vault note as formatted text."""
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        return {"items": ctx.store.list_portfolio_items(user.id, classification=classification)}
    finally:
        cdb.close()


@router.post("/api/career/portfolio/items")
def refresh_portfolio_item(body: PortfolioItemRefreshBody, user: User = Depends(current_user)):
    """Manual on-demand refresh for one repo — same 'manual button, has
    side effects' precedent as GET /api/career/portfolio above. Always
    proposes (tier 2), never applies immediately."""
    cdb, ctx = _ctx(user)
    try:
        from ...career_portfolio import propose_portfolio_update
        item = ctx.store.get_portfolio_item(user.id, body.repo_name)
        if item is None:
            raise HTTPException(status_code=404,
                                detail=f"no portfolio item {body.repo_name!r} on file — "
                                       "run a portfolio analysis first")
        return propose_portfolio_update(ctx, body.repo_name, item.get("why", ""),
                                        item.get("bullets") or [], source="manual_refresh")
    finally:
        cdb.close()


@router.get("/api/career/resume/versions")
def list_resume_versions(user: User = Depends(current_user)):
    """Metadata only — id/label/target_track/created_at/char count, never
    the decrypted content (same 'never return raw resume text over the
    wire' rule get_career_profile above already follows)."""
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        return {"versions": ctx.store.list_resume_versions(user.id)}
    finally:
        cdb.close()


@router.post("/api/career/resume/versions")
def create_resume_version(body: ResumeVersionBody, user: User = Depends(current_user)):
    """Generates a track-specific resume draft and ALWAYS proposes it
    (tier 2) — never auto-saved, regardless of who called this route."""
    cdb, ctx = _ctx(user)
    try:
        from ...career_resume import generate_resume_version
        return generate_resume_version(ctx, body.target_track, label=body.label)
    finally:
        cdb.close()


@router.get("/api/career/resume/performance")
def get_resume_performance(user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        from ...career_resume import resume_performance
        return resume_performance(ctx)
    finally:
        cdb.close()


@router.get("/api/career/opportunities")
def list_career_opportunities(source: str | None = None, user: User = Depends(current_user)):
    """CAREER AUTOPILOT Phase E: discovered hiring signals (HN 'Who's
    Hiring' + GitHub org activity/Product Hunt/Reddit) — stored score/
    reasons only, never recomputed here."""
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        from ...opportunity_radar import list_opportunities
        return {"opportunities": list_opportunities(ctx, source=source)}
    finally:
        cdb.close()


@router.post("/api/career/interviews")
def log_career_interview(body: InterviewLogBody, user: User = Depends(current_user)):
    """CAREER AUTOPILOT Phase F: log an interview — a manual journal
    entry, not a detection system. Auto-executed + notified (tier 1,
    fixed internally regardless of caller)."""
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        from ...interview_memory import log_interview
        result = log_interview(
            ctx, application_id=body.application_id, company=body.company,
            round_type=body.round_type, questions=body.questions,
            self_assessed_outcome=body.self_assessed_outcome,
            weakness_tags=body.weakness_tags, notes=body.notes)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        cdb.close()


@router.get("/api/career/interviews/patterns")
def get_interview_patterns(user: User = Depends(current_user)):
    """CAREER AUTOPILOT Phase F: retrospective pattern analysis over
    logged interviews — never a forecast."""
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        from ...interview_memory import interview_patterns
        return interview_patterns(ctx)
    finally:
        cdb.close()


@router.get("/api/career/companies")
def list_career_companies(city: str | None = None, confidence: str | None = None,
                          is_target: bool | None = None, user: User = Depends(current_user)):
    """Company Discovery extension: free-sources-only ATS/Himalayas/
    TheirStack/GitHub fan-out results."""
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        from ...company_discovery import list_companies
        return {"companies": list_companies(ctx, city=city, confidence=confidence,
                                            is_target=is_target)}
    finally:
        cdb.close()


@router.patch("/api/career/companies/{company_id}/target")
def set_career_company_target(company_id: str, body: CompanyTargetBody,
                              user: User = Depends(current_user)):
    """Toggle whether a discovered company is fast-tracked by the hourly
    ats_fast_poll job."""
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        from ...company_discovery import set_company_target
        if not set_company_target(ctx, company_id, body.is_target):
            raise HTTPException(status_code=404, detail="company not found")
        return {"ok": True}
    finally:
        cdb.close()


@router.get("/api/career/companies/{company_id}/postings")
def get_career_company_postings(company_id: str, user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        from ...company_discovery import company_postings
        return {"postings": company_postings(ctx, company_id)}
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


# ---------------------------------------------------------------------------
# JD Match Advisor — paste a JD, get a grounded match report against the
# saved resume. See amy/jd_match.py's module docstring for the scope note
# (adapted from a resume-versioning brief this codebase never built).
# ---------------------------------------------------------------------------

class JdAnalyzeBody(BaseModel):
    jd_text: str
    job_posting_id: str | None = None


@router.post("/api/career/jd/analyze")
def analyze_jd_route(body: JdAnalyzeBody, user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        from ...jd_match import analyze_jd
        result = analyze_jd(ctx, body.jd_text, job_posting_id=body.job_posting_id)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result
    finally:
        cdb.close()


@router.get("/api/career/jd/analyses")
def list_jd_analyses(limit: int = 20, user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        return {"analyses": ctx.store.list_jd_analyses(user.id, limit=limit)}
    finally:
        cdb.close()


@router.get("/api/career/jd/analyses/{analysis_id}")
def get_jd_analysis(analysis_id: str, user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        a = ctx.store.get_jd_analysis(user.id, analysis_id)
        if a is None:
            raise HTTPException(status_code=404, detail="analysis not found")
        return a
    finally:
        cdb.close()
