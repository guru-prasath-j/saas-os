"""Journaling bridge (Phase 2) — connect the Event Bus to the MemoryWriter.

Two complementary paths:

* ``attach_journal(event_store, vault_path)`` — *push*. Subscribes the writer to
  every event ('*') on a long-lived bus, so events are journaled the instant they
  are emitted. Use where one EventStore lives for the process lifetime.

* ``JournalSync(collab_db, vault_path)`` — *pull*. Reads the persisted `events`
  table and journals anything not yet written. This is the reliable path for the
  SaaS layer, where EventStore instances are created per-request: every emit is
  still persisted to the table, and ``sync()`` catches up. A cursor is kept in
  ``prefs`` for efficiency, but correctness relies on the writer's vault-native
  idempotency markers, so re-running ``sync()`` is always safe.

Phase 3: both paths build an EntityIndex (from notes + goals) so journal entries
are auto-linked to known vault notes/goals.
"""
from __future__ import annotations

import json

from .writer import MemoryWriter
from .entities import EntityIndex

CURSOR_KEY = "memory.journal_cursor"  # last journaled event id (in prefs)


def attach_journal(event_store, vault_path, notes=None, collab_db=None):
    """Subscribe a MemoryWriter to all events on this bus. Returns the handler
    (so it can be unsubscribed). If notes/collab_db are given, entries are
    auto-linked to known entities (Phase 3)."""
    idx = EntityIndex.from_sources(notes=notes, collab_db=collab_db) \
        if (notes or collab_db) else None
    writer = MemoryWriter(vault_path, entity_index=idx)

    def _handler(ev):
        try:
            writer.log_event(ev)
        except Exception:
            pass  # journaling must never break the emitter

    event_store.subscribe("*", _handler)
    return _handler


class JournalSync:
    def __init__(self, collab_db, vault_path, notes=None):
        self.db = collab_db.conn
        idx = EntityIndex.from_sources(notes=notes, collab_db=collab_db)
        self.writer = MemoryWriter(vault_path, entity_index=idx)

    def _cursor(self) -> str | None:
        r = self.db.execute("SELECT value FROM prefs WHERE key=?", (CURSOR_KEY,)).fetchone()
        return r["value"] if r else None

    def _set_cursor(self, eid: str):
        self.db.execute("INSERT OR REPLACE INTO prefs (key, value) VALUES (?,?)",
                        (CURSOR_KEY, eid))
        self.db.commit()

    def sync(self, limit: int = 1000) -> dict:
        """Journal all events not yet written. Idempotent. Returns counts."""
        rows = self.db.execute(
            "SELECT id, ts, type, payload, source FROM events ORDER BY ts ASC LIMIT ?",
            (limit,)).fetchall()
        written = skipped = atomic = 0
        last = None
        for r in rows:
            ev = {"id": r["id"], "ts": r["ts"], "type": r["type"],
                  "payload": json.loads(r["payload"] or "{}"), "source": r["source"]}
            res = self.writer.log_event(ev)
            if res["daily"]:
                written += 1
            else:
                skipped += 1
            if res["atomic"]:
                atomic += 1
            last = r["id"]
        if last:
            self._set_cursor(last)
        return {"written": written, "skipped": skipped, "atomic_notes": atomic,
                "scanned": len(rows)}
