"""StateStore (OL-1) — live entity registry + connector state.

Reuses the existing per-user ``collab.db`` (no new database). Two tables:
  * ``op_entities``        — current snapshot of each external entity
  * ``op_connector_state`` — per-connector status / health / sync cursor

This is the "what is true right now" half the event log alone can't provide.
``EntityRegistry`` is exposed as an alias of the entity-facing API.
"""
from __future__ import annotations

import datetime as _dt

from .models import EntityState


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class StateStore:
    def __init__(self, collab_db):
        self.db = collab_db.conn

    # --- entities -------------------------------------------------------
    def upsert_entity(self, entity: EntityState) -> EntityState:
        entity.updated_at = _now()
        self.db.execute(
            "INSERT INTO op_entities (entity_id, kind, source, title, state, updated_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(entity_id) DO UPDATE SET kind=excluded.kind, source=excluded.source, "
            "title=excluded.title, state=excluded.state, updated_at=excluded.updated_at",
            entity.to_row())
        self.db.commit()
        return entity

    def get_entity(self, entity_id: str) -> EntityState | None:
        r = self.db.execute("SELECT * FROM op_entities WHERE entity_id=?", (entity_id,)).fetchone()
        return EntityState.from_row(r) if r else None

    def list_entities(self, kind: str | None = None, source: str | None = None,
                      limit: int = 200) -> list[EntityState]:
        q = "SELECT * FROM op_entities"
        conds, args = [], []
        if kind:
            conds.append("kind=?"); args.append(kind)
        if source:
            conds.append("source=?"); args.append(source)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY updated_at DESC LIMIT ?"; args.append(limit)
        return [EntityState.from_row(r) for r in self.db.execute(q, args).fetchall()]

    def delete_entity(self, entity_id: str) -> bool:
        cur = self.db.execute("DELETE FROM op_entities WHERE entity_id=?", (entity_id,))
        self.db.commit()
        return cur.rowcount > 0

    def count_entities(self) -> int:
        return self.db.execute("SELECT COUNT(*) c FROM op_entities").fetchone()["c"]

    # --- connector state ------------------------------------------------
    def set_connector_state(self, connector: str, status: str | None = None,
                            health: str | None = None, last_sync: str | None = None,
                            cursor: str | None = None, detail: str | None = None) -> dict:
        cur = self.get_connector_state(connector) or {
            "connector": connector, "status": "registered", "health": "unknown",
            "last_sync": None, "cursor": None, "detail": None}
        if status is not None: cur["status"] = status
        if health is not None: cur["health"] = health
        if last_sync is not None: cur["last_sync"] = last_sync
        if cursor is not None: cur["cursor"] = cursor
        if detail is not None: cur["detail"] = detail
        self.db.execute(
            "INSERT INTO op_connector_state (connector, status, health, last_sync, cursor, detail) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(connector) DO UPDATE SET status=excluded.status, health=excluded.health, "
            "last_sync=excluded.last_sync, cursor=excluded.cursor, detail=excluded.detail",
            (cur["connector"], cur["status"], cur["health"], cur["last_sync"],
             cur["cursor"], cur["detail"]))
        self.db.commit()
        return cur

    def get_connector_state(self, connector: str) -> dict | None:
        r = self.db.execute("SELECT * FROM op_connector_state WHERE connector=?",
                            (connector,)).fetchone()
        return dict(r) if r else None

    def all_connector_states(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM op_connector_state ORDER BY connector").fetchall()]


# EntityRegistry is the entity-facing view of the StateStore (same object/API).
EntityRegistry = StateStore
