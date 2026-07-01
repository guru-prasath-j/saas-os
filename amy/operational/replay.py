"""ReplayService (OL-4) — generic event replay over the existing events table.

Re-dispatches persisted events to a handler, optionally filtered by type and
starting after a cursor. This generalizes the cursor pattern that
``amy.memory.journal.JournalSync`` implements ad-hoc, so any new subscriber
(an agent, a projection, the memory writer) can backfill deterministically.

It reads the ONE event log (``events`` table) — it does not create a second
store or bus.
"""
from __future__ import annotations

import json


class ReplayService:
    def __init__(self, collab_db):
        self.db = collab_db.conn

    def events(self, since_ts: str | None = None, types: list[str] | None = None,
               limit: int = 1000) -> list[dict]:
        q = "SELECT id, ts, type, payload, source FROM events"
        conds, args = [], []
        if since_ts:
            conds.append("ts > ?"); args.append(since_ts)
        if types:
            conds.append("type IN (%s)" % ",".join("?" * len(types))); args += types
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY ts ASC LIMIT ?"; args.append(limit)
        return [{"id": r["id"], "ts": r["ts"], "type": r["type"],
                 "payload": json.loads(r["payload"] or "{}"), "source": r["source"]}
                for r in self.db.execute(q, args).fetchall()]

    def replay(self, handler, since_ts: str | None = None,
               types: list[str] | None = None, limit: int = 1000) -> dict:
        """Call handler(event) for each matching event in time order. A failing
        handler on one event never aborts the rest. Returns counts + last ts."""
        evs = self.events(since_ts=since_ts, types=types, limit=limit)
        ok = err = 0
        last_ts = since_ts
        for ev in evs:
            try:
                handler(ev); ok += 1
            except Exception:
                err += 1
            last_ts = ev["ts"]
        return {"dispatched": ok, "errors": err, "scanned": len(evs), "last_ts": last_ts}
