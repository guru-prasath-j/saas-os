"""Automation layer — durable jobs, Approval Inbox, hybrid ingest, sentinels,
monthly close, custodial autopilot, morning briefing, and the AI assistant.

    from amy.automation import build_ctx, run_due, AutomationStore
"""
from .store import AutomationStore, TrackedLLM, compute_next_run
from .executors import JobCtx, submit_action, approve, reject, execute, agent_gate
from .jobs import build_ctx, ensure_defaults, run_due, run_job, HANDLERS, DEFAULT_JOBS

# R3: install the approval gate — from here on, any registry write/destructive
# tool invoked with actor="agent" parks in the Approval Inbox instead of
# executing. This import-time hookup is what makes the trust boundary
# architectural: there is no code path where an agent bypasses it.
from ..tools import registry as _tool_registry
_tool_registry.AGENT_GATE = agent_gate
