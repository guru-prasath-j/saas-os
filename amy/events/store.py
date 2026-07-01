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
AGENT_TOGGLED = "agent.toggled"
DIGEST_GENERATED = "digest.generated"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class EventStore:
    def __init__(self, collab_db):
        self.db = collab_db.conn
        self._handlers: dict[str, list] = {}

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
                pass   # a bad subscriber never breaks the emitter
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
