"""Event store + in-process event bus.

Upgrades the previous "activity log only" state into a first-class event layer:
events are persisted (events table in collab.db) AND dispatched to subscribers at
emit time, so triggers (reflection/learning/scheduler) can react.
"""
from __future__ import annotations

import datetime as _dt
import inspect
import json
import logging
import threading
import uuid

_log = logging.getLogger("amy.events")

# canonical event types emitted across the system
QUERY_ASKED = "query.asked"
GOAL_CREATED = "goal.created"
GOAL_COMPLETED = "goal.completed"
CAPTURE_ADDED = "capture.added"
VAULT_IMPORTED = "vault.imported"
VAULT_NOTE_EDITED = "vault.note_edited"
AGENT_TOGGLED = "agent.toggled"
DIGEST_GENERATED = "digest.generated"

# Finance events
FINANCE_TRANSACTION_ADDED = "finance.transaction_added"
FINANCE_CSV_IMPORTED = "finance.csv_imported"
FINANCE_PDF_IMPORTED = "finance.pdf_imported"
FINANCE_GMAIL_SYNCED = "finance.gmail_synced"
FINANCE_BUDGET_SET = "finance.budget_set"
FINANCE_SUBSCRIPTION_ADDED = "finance.subscription_added"
FINANCE_INVESTMENT_ADDED = "finance.investment_added"
FINANCE_INCOME_ADDED = "finance.income_added"

# Agent events (reactive agents / orchestrator / screening)
AGENT_INSIGHT = "agent.insight"
AGENT_ACTION_PROPOSED = "agent.action_proposed"
AGENT_ACTION_EXECUTED = "agent.action_executed"
AGENT_ERROR = "agent.error"

# Context / physical-world sensor events (docs/CONTEXT_PLAN.md C1)
CONTEXT_LOCATION_UPDATED = "context.location_updated"
CONTEXT_PLACE_ENTERED = "context.place_entered"
CONTEXT_PLACE_LEFT = "context.place_left"

# Learning feed events
LEARNING_FEED_REFRESHED = "learning.feed_refreshed"
LEARNING_ITEM_COMPLETED = "learning.item_completed"

# Business entity events
BUSINESS_ENTITY_CREATED = "business.entity_created"
FINANCE_LEDGER_ENTRY_POSTED = "finance.ledger_entry_posted"
FINANCE_LEDGER_AUDITED = "finance.ledger_audited"
FINANCE_COMPLIANCE_SUGGESTED = "finance.compliance_suggested"

# Connector events (CONNECTOR COMPLETION phase) — GitHub/Plane sensors
GITHUB_PR_REVIEW_REQUESTED = "github.pr_review_requested"
GITHUB_PR_STATUS_CHANGED = "github.pr_status_changed"
GITHUB_ISSUE_ASSIGNED = "github.issue_assigned"
PLANE_TASK_ASSIGNED = "plane.task_assigned"
PLANE_TASK_DUE_SOON = "plane.task_due_soon"
PLANE_TASK_STATUS_CHANGED = "plane.task_status_changed"

# Career events (CAREER AUTOPILOT phase)
CAREER_GOAL_SET = "career.goal_set"
CAREER_JOB_DISCOVERED = "career.job_discovered"
CAREER_APPLICATION_PREPARED = "career.application_prepared"
CAREER_APPLICATION_SENT = "career.application_sent"
CAREER_APPLICATION_STATUS_CHANGED = "career.application_status_changed"
CAREER_PORTFOLIO_ANALYZED = "career.portfolio_analyzed"
CAREER_JD_ANALYZED = "career.jd_analyzed"

# Life events (LIFE AUTOPILOT phase) — payloads are metric keys/counts only,
# never coordinates or raw health values (privacy floor, docs/LIFE_AUTOPILOT.md).
LIFE_METRICS_COMPUTED = "life.metrics_computed"        # L2
LIFE_PATTERN_DETECTED = "life.pattern_detected"        # L3
LIFE_HABIT_AUTOCOMPLETED = "life.habit_autocompleted"  # L4
LIFE_WELLBEING_WEEK_COMPUTED = "life.wellbeing_week_computed"  # L5

# Fraud Detection Module (Phase 1, amy/finance/fraud_engine.py). Payload is
# {transaction_id, risk_level, recommended_action, reason_code_count} —
# never the raw score breakdown. Not yet in AGENT_RELEVANT_EVENTS below —
# no reactive agent subscribes to it in Phase 1 (same precedent as
# career.* events, which were only added to that warn-set once a real
# subscriber existed).
FRAUD_DETECTED = "fraud.detected"

# AML Monitoring Module (Phase 2, amy/finance/aml_engine.py). aml.alert
# fires on every detector trigger (even a re-confirmed existing case);
# aml.case_opened fires only when a NEW aml_cases row was created. Payloads
# are {case_id, typology, risk_level[, evidence_count]} — never raw
# transaction amounts/merchants. Not yet in AGENT_RELEVANT_EVENTS — no
# reactive agent subscribes to either in Phase 2 (same precedent as
# fraud.detected above).
AML_ALERT = "aml.alert"
AML_CASE_OPENED = "aml.case_opened"

# Amy Credit Score Module (Phase 3, amy/finance/credit_engine.py) — an
# illustrative internal score, never a real bureau product. Payload is
# {score, computed_at} only. Not yet in AGENT_RELEVANT_EVENTS — no
# reactive agent subscribes yet (same precedent as the two above).
CREDIT_UPDATED = "credit.updated"

# Loan Underwriting Module (Phase 5, amy/finance/loan_engine.py) — an
# illustrative underwriting simulation, never a real lending decision.
# loan.requested fires when apply_for_loan() persists a new application
# (before the human decides); loan.approved fires from the loan_decision
# executor once a human approves the tier-2 request. loan.rejected is
# defined for documentation/future use but no code path emits it today —
# rejection is handled by lazy reconciliation against the approvals table
# on read (see loan_engine._reconcile()'s docstring) rather than a
# dedicated executor, so there's no natural emit site yet; add one if a
# future reactive agent needs to react to a rejection specifically. Not
# yet in AGENT_RELEVANT_EVENTS — no reactive agent subscribes yet (same
# precedent as the three above).
LOAN_REQUESTED = "loan.requested"
LOAN_APPROVED = "loan.approved"
LOAN_REJECTED = "loan.rejected"

# CAREER AUTOPILOT Phase A ("Learning Driven by Jobs", amy/career_scout.py's
# skill_demand_report()). A genuinely different concern from
# LEARNING_FEED_REFRESHED (feed ITEMS being fetched for an existing focus) —
# this fires when a market-demand report over job_postings.keywords is
# computed. Payload: {track, postings_analyzed, top_skill}. Not yet in
# AGENT_RELEVANT_EVENTS — no reactive agent subscribes yet.
CAREER_SKILL_DEMAND_UPDATED = "career.skill_demand_updated"

# CAREER AUTOPILOT Phase C (amy/career_sprint.py) — the weekly sprint
# generate/review loop. sprint_generated payload: {week, status,
# skill_gaps_addressed, skill_gaps_total}; sprint_reviewed payload: {week,
# tasks_completed, tasks_planned, applications_sent, interviews_scheduled}.
# Not yet in AGENT_RELEVANT_EVENTS — no reactive agent subscribes yet
# (same precedent as CAREER_SKILL_DEMAND_UPDATED above).
CAREER_SPRINT_GENERATED = "career.sprint_generated"
CAREER_SPRINT_REVIEWED = "career.sprint_reviewed"

# CAREER AUTOPILOT Phase E (amy/opportunity_radar.py) — fires once per
# newly-scored opportunity (HN "Who is Hiring" posting or a company-level
# opportunity_signals row). Payload: {source, company, score} only. Not
# yet in AGENT_RELEVANT_EVENTS — no reactive agent subscribes yet, same
# precedent as CAREER_SKILL_DEMAND_UPDATED/CAREER_SPRINT_GENERATED.
CAREER_OPPORTUNITY_DETECTED = "career.opportunity_detected"

# CAREER AUTOPILOT Phase F (amy/interview_memory.py, final phase) — a
# manually-logged journal entry, not a detection event. Payload:
# {application_id, company, round_type, self_assessed_outcome}. Not yet
# in AGENT_RELEVANT_EVENTS — no reactive agent subscribes yet, same
# precedent as every other Phase A-E career event.
CAREER_INTERVIEW_LOGGED = "career.interview_logged"

# Company Discovery + ATS Fast-Track (extends CAREER AUTOPILOT Phase E,
# amy/company_discovery.py). Fires once per NEW posting discovered via a
# direct Greenhouse/Lever/Ashby ATS poll. Payload: {posting_id, company,
# platform}. Not yet in AGENT_RELEVANT_EVENTS — no reactive agent
# subscribes yet, same precedent as every other career event.
CAREER_JOB_POSTING_DETECTED_FAST = "career.job_posting_detected_fast"

# Event types a reactive agent (amy/agents/reactive.py) actually .subscribe()s
# to today. Kept as a plain literal set HERE rather than imported from
# amy.agents.reactive, so this module stays import-free of agents/automation
# (see amy/events/factory.py's RISK A note) — update this set whenever a new
# agent subscription is added in reactive.py. Used only for the zero-
# subscriber dev warning below; never gates emit() itself.
AGENT_RELEVANT_EVENTS = frozenset({
    FINANCE_TRANSACTION_ADDED, FINANCE_CSV_IMPORTED, FINANCE_PDF_IMPORTED,
    FINANCE_GMAIL_SYNCED, FINANCE_LEDGER_ENTRY_POSTED,
    CONTEXT_PLACE_ENTERED, CONTEXT_PLACE_LEFT,
    LEARNING_FEED_REFRESHED, LEARNING_ITEM_COMPLETED,
    GITHUB_PR_REVIEW_REQUESTED, GITHUB_PR_STATUS_CHANGED,
})

_warned_zero_subscriber_sites: set[tuple] = set()
_warn_lock = threading.Lock()


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class EventStore:
    def __init__(self, collab_db):
        self.db = collab_db.conn
        self._handlers: dict[str, list] = {}
        # Idempotent-registration guard (Part 0 / quirk 20 fix): tracks which
        # reactive agents (by name) are already wired onto THIS instance, so
        # amy.agents.reactive.register_reactive_agents can no-op on a repeat
        # call instead of double-subscribing the same agent (which would
        # double-fire it, and for non-deduped agents produce duplicate
        # agent.insight events / duplicate approval rows).
        self._registered_agent_keys: set[str] = set()
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS event_dead_letters ("
            " id TEXT PRIMARY KEY, ts TEXT, event_id TEXT, event_type TEXT,"
            " handler TEXT, error TEXT, retries INTEGER DEFAULT 0)")
        self.db.commit()

    # --- pub/sub -----------------------------------------------------------
    def subscribe(self, event_type: str, handler):
        """handler(event_dict) is called synchronously on emit/publish. Use '*' for all."""
        self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler) -> bool:
        """Remove a previously-subscribed handler. Returns True if removed."""
        lst = self._handlers.get(event_type)
        if lst and handler in lst:
            lst.remove(handler)
            return True
        return False

    def publish(self, event_type: str, payload: dict | None = None, source: str = "") -> str:
        """Alias for emit() — the canonical event-bus verb."""
        return self.emit(event_type, payload, source)

    def emit(self, event_type: str, payload: dict | None = None, source: str = "") -> str:
        eid = uuid.uuid4().hex[:12]
        ts = _now()
        self.db.execute(
            "INSERT INTO events (id, ts, type, payload, source) VALUES (?,?,?,?,?)",
            (eid, ts, event_type, json.dumps(payload or {}), source))
        self.db.commit()
        ev = {"id": eid, "ts": ts, "type": event_type, "payload": payload or {}, "source": source}
        handlers = list(self._handlers.get(event_type, [])) + list(self._handlers.get("*", []))
        if not handlers and event_type in AGENT_RELEVANT_EVENTS:
            self._warn_zero_subscribers(event_type)
        for fn in handlers:
            try:
                fn(ev)
            except Exception:
                # a bad subscriber never breaks the emitter: retry once,
                # then record the failure as a dead letter instead of losing it
                try:
                    fn(ev)
                except Exception as exc:
                    try:
                        self.db.execute(
                            "INSERT INTO event_dead_letters"
                            " (id, ts, event_id, event_type, handler, error, retries)"
                            " VALUES (?,?,?,?,?,?,1)",
                            (uuid.uuid4().hex[:12], _now(), eid, event_type,
                             getattr(fn, "__qualname__", repr(fn)), str(exc)[:400]))
                        self.db.commit()
                    except Exception:
                        pass
        return eid

    def _warn_zero_subscribers(self, event_type: str) -> None:
        """Dev guardrail: an agent-relevant event type emitted on an instance
        with zero subscribers usually means a bare EventStore(cdb) was built
        instead of amy.events.factory.get_events() — loud (one log line per
        process per call-site), not silent. Never raises; a broken warning
        must not affect the emit it's warning about."""
        try:
            frame = inspect.stack()[2]   # 0=this fn, 1=emit(), 2=emit()'s caller
            site = (frame.filename, frame.lineno)
        except Exception:
            site = ("<unknown>", 0)
        key = (site, event_type)
        with _warn_lock:
            if key in _warned_zero_subscriber_sites:
                return
            _warned_zero_subscriber_sites.add(key)
        _log.warning(
            "EventStore.emit(%r) from %s:%d has ZERO subscribers on this "
            "instance. If this event type is meant to trigger a reactive "
            "agent, this store was likely built bare instead of via "
            "amy.events.factory.get_events() (see CLAUDE.md quirk 20).",
            event_type, site[0], site[1])

    # --- reads -------------------------------------------------------------
    def recent(self, event_type: str | None = None, n: int = 50) -> list[dict]:
        if event_type:
            rs = self.db.execute(
                "SELECT id,ts,type,payload,source FROM events WHERE type=? ORDER BY ts DESC LIMIT ?",
                (event_type, n)).fetchall()
        else:
            rs = self.db.execute(
                "SELECT id,ts,type,payload,source FROM events ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
        return [{"id": r["id"], "ts": r["ts"], "type": r["type"],
                 "payload": json.loads(r["payload"] or "{}"), "source": r["source"]} for r in rs]

    def stats(self) -> dict:
        rs = self.db.execute("SELECT type, COUNT(*) c FROM events GROUP BY type").fetchall()
        return {r["type"]: r["c"] for r in rs}

    def dead_letters(self, n: int = 50) -> list[dict]:
        rs = self.db.execute(
            "SELECT * FROM event_dead_letters ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rs]
