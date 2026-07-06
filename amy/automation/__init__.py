"""Automation layer — durable jobs, Approval Inbox, hybrid ingest, sentinels,
monthly close, custodial autopilot, morning briefing, and the AI assistant.

    from amy.automation import build_ctx, run_due, AutomationStore
"""
from .store import AutomationStore, TrackedLLM, compute_next_run
from .executors import JobCtx, submit_action, approve, reject, execute
from .jobs import build_ctx, ensure_defaults, run_due, run_job, HANDLERS, DEFAULT_JOBS
