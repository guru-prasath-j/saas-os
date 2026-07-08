"""Event store + in-process event bus.

Upgrades the previous "activity log only" state into a first-class event layer:
events are persisted (events table in collab.db) AND dispatched to subscribers at
emit time, so triggers (reflection/learning/scheduler) can react.
"""
from __future__ import annotations

import datetime as _dt
import json
import uuid

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

# Learning feed events
LEARNING_FEED_REFRESHED = "learning.feed_refreshed"

# Business entity events
BUSINESS_ENTITY_CREATED = "business.entity_created"
FINANCE_LEDGER_ENTRY_POSTED = "finance.ledger_entry_posted"
FINANCE_LEDGER_AUDITED = "finance.ledger_audited"
FINANCE_COMPLIANCE_SUGGESTED = "finance.compliance_suggested"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class EventStore:
    def __init__(self, collab_db):
        self.db = collab_db.conn
        self._handlers: dict[str, list] = {}
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
        for fn in list(self._handlers.get(event_type, [])) + list(self._handlers.get("*", [])):
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
