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
}

def _default_jobs() -> list[tuple[str, dict]]:
    """Env-configurable initial schedules (config.py pattern — remember
    .env.personal loads first with override=False). Existing job rows are
    never overridden; edit via PATCH /api/automation/jobs/{name}."""
    from .. import config
    briefing_at = config._env("AMY_BRIEFING_AT", "07:00")
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
