"""Job registry + per-user runner.

The app-level loop (amy/saas/app.py _automation_loop) ticks every minute,
builds a JobCtx per user, and calls run_due(). Every run is recorded in the
automation_runs ledger — no silent failures.
"""
from __future__ import annotations

from . import closers, ingest, learning, sentinels
from .capture_digest import capture_digest
from ..learning_feed.sensor import learning_feed_refresh
from .executors import JobCtx
from .store import AutomationStore, TrackedLLM


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _auto_categorize(ctx: JobCtx) -> dict:
    from ..finance.categorizer import auto_categorize_all
    fe = ctx.open_finance()
    try:
        learned = learning.apply_learned_rules(fe)
        updated = auto_categorize_all(fe, llm=ctx.llm)
        return {"learned_rules_applied": learned, "recategorized": updated}
    finally:
        fe.close()


def _obligation_check(ctx: JobCtx) -> dict:
    from ..obligations.agent import obligation_check
    return obligation_check(ctx)


def _place_learning(ctx: JobCtx) -> dict:
    from ..geo.learn import place_learning
    return place_learning(ctx)


def _commitment_scan(ctx: JobCtx) -> dict:
    from ..commitments import commitment_scan
    return commitment_scan(ctx)


def _pattern_tasks(ctx: JobCtx) -> dict:
    from ..patterns import pattern_tasks
    return pattern_tasks(ctx)


def _relationship_nudges(ctx: JobCtx) -> dict:
    from ..patterns import relationship_nudges
    return relationship_nudges(ctx)


def _preference_drift(ctx: JobCtx) -> dict:
    from .drift import preference_drift
    return preference_drift(ctx)


def _meeting_prep_scan(ctx: JobCtx) -> dict:
    """CONNECTOR COMPLETION Part 2: drives the meeting_prep agent's window
    check every 15 min — cheap (one Google Calendar list call plus, only
    for meetings actually inside the prep window, a couple of read tool
    calls for keyword-matching)."""
    from ..agents.reactive import meeting_prep_check
    n = meeting_prep_check(ctx.events(), ctx)
    return {"meetings_prepped": n}


def _career_goal_stall_check(ctx: JobCtx) -> dict:
    """CAREER AUTOPILOT Part 2: drives the career_goal agent's stall-nudge
    check daily — 'N days of silence' has no natural push event, same
    structural choice as meeting_prep_scan above."""
    from ..agents.reactive import career_goal_stall_check
    return career_goal_stall_check(ctx.events(), ctx)


def _portfolio_review(ctx: JobCtx) -> dict:
    """CAREER AUTOPILOT Part 3: monthly portfolio re-analysis for whichever
    career-domain goal is active (skipped, not an error, if there isn't
    one — most users won't have an active career goal most months)."""
    from ..agents.reactive import portfolio_analyze
    row = ctx.collab.conn.execute(
        "SELECT id FROM goals WHERE domain='career' AND status='active'"
        " ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        return {"skipped": "no active career goal"}
    return portfolio_analyze(ctx.events(), ctx, goal_id=row["id"])


def _job_scout_poll(ctx: JobCtx) -> dict:
    """CAREER AUTOPILOT Part 4: drives JobScoutSensor on the interval below
    (default 12h, AMY_JOB_SCOUT_INTERVAL_HOURS) — no-ops cleanly when there
    is no active career goal (see JobScoutSensor.poll)."""
    from ..career_scout import job_scout_poll
    return job_scout_poll(ctx)


def _application_followup_check(ctx: JobCtx) -> dict:
    """CAREER AUTOPILOT Part 5: staleness follow-up + ghosting, every 2 days."""
    from ..career_apply import followup_check
    return followup_check(ctx)


def _interview_debrief_scan(ctx: JobCtx) -> dict:
    """CAREER AUTOPILOT Part 5E: prompts once for a debrief after a
    career-linked calendar event ends — polling-driven like
    meeting_prep_scan because 'a meeting just ended' has no push event.
    No-ops instantly with no interview-stage applications."""
    from ..agents.reactive import interview_debrief_check
    return {"prompted": interview_debrief_check(ctx.events(), ctx)}


def _career_retention(ctx: JobCtx) -> dict:
    """CAREER AUTOPILOT Part 5E: monthly hygiene — archive
    discovered/dismissed postings older than AMY_CAREER_RETENTION_DAYS
    (default 90) that never became an application, and compact their
    career.job_discovered event rows. Applications are NEVER deleted:
    outcome learning depends on full history."""
    import datetime as _dt

    from .. import config
    try:
        days = int(config._env("AMY_CAREER_RETENTION_DAYS", "90"))
    except ValueError:
        days = 90
    cutoff = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(days=days)).isoformat()
    applied_pids = {a["posting_id"]
                    for a in ctx.store.list_applications(ctx.user_id)}
    archived = compacted = 0
    for p in ctx.store.list_postings(ctx.user_id, limit=10000):
        if p["status"] not in ("discovered", "dismissed"):
            continue
        if (p.get("discovered_at") or "") >= cutoff:
            continue
        if p["id"] in applied_pids:
            continue   # became an application — its posting stays queryable
        if ctx.store.set_posting_status(ctx.user_id, p["id"], "archived"):
            archived += 1
            cur = ctx.collab.conn.execute(
                "DELETE FROM events WHERE type='career.job_discovered'"
                " AND payload LIKE ?", (f'%{p["id"]}%',))
            compacted += cur.rowcount
    ctx.collab.conn.commit()
    return {"archived": archived, "events_compacted": compacted}


def _connector_sensor_scan(ctx: JobCtx) -> dict:
    """CONNECTOR COMPLETION Part 2: drives GitHubSensor/PlaneSensor.poll()
    on the interval below (poll_hours configurable via
    AMY_CONNECTOR_SENSOR_INTERVAL_HOURS — the 'poll intervals
    env-configurable' requirement). Each sensor independently try/excepted
    so GitHub being unreachable never blocks Plane polling or vice versa;
    a missing connector for either just makes that sensor's poll() a no-op
    (find_connector_row returns None). Also what Part 3's connectors tab
    'Sync now' button triggers for GitHub/Plane."""
    from ..connectors.sensors import GitHubSensor, PlaneSensor
    out = {"github": 0, "plane": 0, "errors": []}
    events = ctx.events()
    try:
        out["github"] = len(GitHubSensor(events, ctx).poll())
    except Exception as exc:
        out["errors"].append(f"github: {exc}"[:200])
    try:
        out["plane"] = len(PlaneSensor(events, ctx).poll())
    except Exception as exc:
        out["errors"].append(f"plane: {exc}"[:200])
    return out


HANDLERS: dict[str, callable] = {
    "gmail_statement_ingest": ingest.gmail_statement_ingest,
    "auto_categorize": _auto_categorize,
    "anomaly_sentinel": sentinels.anomaly_sentinel,
    "cashflow_alerts": sentinels.cashflow_alerts,
    "monthly_close": closers.monthly_close,
    "custodial_autopilot": closers.custodial_autopilot,
    "morning_briefing": closers.morning_briefing,
    "autopilot": closers.autopilot_run,
    "obligation_check": _obligation_check,
    "capture_digest": capture_digest,
    "learning_feed_refresh": learning_feed_refresh,
    "place_learning": _place_learning,
    "commitment_scan": _commitment_scan,
    "pattern_tasks": _pattern_tasks,
    "relationship_nudges": _relationship_nudges,
    "preference_drift": _preference_drift,
    "meeting_prep_scan": _meeting_prep_scan,
    "connector_sensor_scan": _connector_sensor_scan,
    "career_goal_stall_check": _career_goal_stall_check,
    "portfolio_review": _portfolio_review,
    "job_scout_poll": _job_scout_poll,
    "application_followup_check": _application_followup_check,
    "interview_debrief_scan": _interview_debrief_scan,
    "career_retention": _career_retention,
}

def _default_jobs() -> list[tuple[str, dict]]:
    """Env-configurable initial schedules (config.py pattern — remember
    .env.personal loads first with override=False). Existing job rows are
    never overridden; edit via PATCH /api/automation/jobs/{name}."""
    from .. import config
    briefing_at = config._env("AMY_BRIEFING_AT", "07:00")
    try:
        sensor_interval_hours = float(
            config._env("AMY_CONNECTOR_SENSOR_INTERVAL_HOURS", "0.5"))
    except ValueError:
        sensor_interval_hours = 0.5
    try:
        job_scout_interval_hours = float(
            config._env("AMY_JOB_SCOUT_INTERVAL_HOURS", "12"))
    except ValueError:
        job_scout_interval_hours = 12.0
    jobs = [
        ("gmail_statement_ingest", {"every_hours": 6}),
        ("auto_categorize",        {"every_hours": 12}),
        ("anomaly_sentinel",       {"daily_at": "08:00"}),
        ("cashflow_alerts",        {"daily_at": "08:10"}),
        ("monthly_close",          {"monthly_day": 1, "at": "06:00"}),
        ("custodial_autopilot",    {"daily_at": "07:30"}),
        ("morning_briefing",       {"daily_at": briefing_at}),
        ("autopilot",              {"daily_at": "05:00"}),
        ("obligation_check",       {"daily_at": "07:15"}),
        ("capture_digest",         {"daily_at": "20:30"}),
        ("place_learning",         {"daily_at": "21:00"}),
        ("commitment_scan",        {"daily_at": "08:20"}),
        ("pattern_tasks",          {"daily_at": "06:30"}),
        ("relationship_nudges",    {"daily_at": "09:00"}),
        ("preference_drift",       {"monthly_day": 2, "at": "06:45"}),
        ("meeting_prep_scan",      {"every_hours": 0.25}),
        ("connector_sensor_scan",  {"every_hours": sensor_interval_hours}),
        ("career_goal_stall_check", {"daily_at": "09:30"}),
        ("portfolio_review",       {"monthly_day": 1, "at": "10:00"}),
        ("job_scout_poll",         {"every_hours": job_scout_interval_hours}),
        ("application_followup_check", {"every_hours": 48}),
        ("interview_debrief_scan", {"every_hours": 1}),
        ("career_retention",       {"monthly_day": 3, "at": "06:15"}),
    ]
    # Env-gated: the handler re-checks the flag too, because job rows persist
    # in automation_jobs after the env is turned off (ensure_job never deletes).
    if config._env("AMY_LEARNING_FEED_ENABLED", "false").strip().lower() == "true":
        jobs.append(("learning_feed_refresh", {"every_hours": 6}))
    return jobs


DEFAULT_JOBS: list[tuple[str, dict]] = _default_jobs()


# ---------------------------------------------------------------------------
# Context + runner
# ---------------------------------------------------------------------------

def build_ctx(user_id: str, user_email: str, collab_db, index_dir,
              llm_router=None, jurisdictions: list[str] | None = None,
              language: str | None = None) -> JobCtx:
    """collab_db stays owned by the caller (caller closes it).
    jurisdictions: home-first pack ids (R7B); briefings and obligation
    deadlines read them from ctx._extras."""
    store = AutomationStore(collab_db)
    # per-user "local-only" LLM routing: prefs key llm_local_only='1' forces
    # every call for this user through the sensitive (Ollama-only) path
    local_only = False
    try:
        row = collab_db.conn.execute(
            "SELECT value FROM prefs WHERE key='llm_local_only'").fetchone()
        local_only = bool(row and str(row["value"]) == "1")
    except Exception:
        local_only = False
    llm = (TrackedLLM(llm_router, store, force_local=local_only)
           if llm_router is not None else None)
    ctx = JobCtx(
        user_id=user_id,
        user_email=user_email,
        finance_path=str(index_dir / "finance.db"),
        collab=collab_db,
        store=store,
        connector_dir=index_dir / "connectors",
        llm=llm,
    )
    ctx._extras["jurisdictions"] = jurisdictions or ["india"]
    ctx._extras["language"] = language
    return ctx


def ensure_defaults(store: AutomationStore):
    for name, schedule in DEFAULT_JOBS:
        store.ensure_job(name, schedule)


def run_job(ctx: JobCtx, name: str) -> dict:
    """Run one job now, with run-ledger bookkeeping. Never raises."""
    handler = HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown job {name!r}"}
    rid = ctx.store.start_run(name)
    try:
        detail = handler(ctx) or {}
        ctx.store.finish_run(rid, "ok", detail)
        ctx.store.mark_job_ran(name, "ok")
        return {"run_id": rid, "status": "ok", "detail": detail}
    except Exception as exc:
        ctx.store.finish_run(rid, "error", {"error": str(exc)[:500]})
        ctx.store.mark_job_ran(name, "error")
        return {"run_id": rid, "status": "error", "error": str(exc)[:500]}


def run_due(ctx: JobCtx) -> list[dict]:
    """Run every due job for this user. Respects the global pause switch."""
    ensure_defaults(ctx.store)
    if ctx.store.paused():
        return []
    results = []
    for job in ctx.store.due_jobs():
        out = run_job(ctx, job["name"])
        out["job"] = job["name"]
        results.append(out)
    return results
