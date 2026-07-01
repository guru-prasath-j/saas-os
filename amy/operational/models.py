"""Operational Layer models (OL-1).

Shared, neutral shapes so the rest of the system never depends on a connector's
raw payload format.

* ``EntityState`` — a live snapshot of one external entity (a repo, an email
  thread, a calendar event, a financial account, …). This is the "what is true
  now" record the event log alone can't answer.
* ``OperationalEvent`` — a thin, typed view over an event dict. It does NOT
  replace ``amy.events.EventStore``; it's just a convenience wrapper so producers
  build consistent payloads before publishing on the one existing bus.
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


@dataclass
class EntityState:
    entity_id: str            # stable id, e.g. "github:repo:me/piOS"
    kind: str                 # "repo" | "thread" | "event" | "account" | ...
    source: str               # "github" | "email" | "calendar" | ...
    title: str = ""
    state: dict = field(default_factory=dict)   # arbitrary current attributes
    updated_at: str = field(default_factory=_now)

    def to_row(self):
        return (self.entity_id, self.kind, self.source, self.title,
                json.dumps(self.state or {}), self.updated_at)

    @staticmethod
    def from_row(r) -> "EntityState":
        return EntityState(
            entity_id=r["entity_id"], kind=r["kind"], source=r["source"],
            title=r["title"] or "", state=json.loads(r["state"] or "{}"),
            updated_at=r["updated_at"] or "")

    def to_dict(self) -> dict:
        return {"entity_id": self.entity_id, "kind": self.kind, "source": self.source,
                "title": self.title, "state": self.state, "updated_at": self.updated_at}


@dataclass
class OperationalEvent:
    type: str                 # canonical event type, e.g. "github.NEW_COMMIT"
    payload: dict = field(default_factory=dict)
    source: str = ""
    ts: str = field(default_factory=_now)

    def publish(self, event_store) -> str:
        """Publish on the ONE existing bus. Returns the event id."""
        return event_store.publish(self.type, self.payload, source=self.source)
