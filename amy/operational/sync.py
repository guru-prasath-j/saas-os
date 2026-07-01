"""SyncService (OL-4) — reconcile external truth into live state.

Pulls current items from a connector (or accepts already-normalized entities),
upserts them into the live entity registry (``op_entities``), detects deltas
against the previous snapshot, and publishes change events on the ONE existing
bus. This is what keeps "what is true now" correct as connectors change — the
piece the event log alone could not provide.

Connectors stay decoupled: SyncService depends on the connector registry and the
StateStore, never on memory or agents. Agents react by subscribing to the bus.
"""
from __future__ import annotations

import datetime as _dt

from .models import EntityState


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class SyncService:
    def __init__(self, state_store, event_store, connector_manager=None):
        self.state = state_store
        self.events = event_store
        self.connectors = connector_manager

    # --- generic reconcile ---------------------------------------------
    def reconcile(self, source: str, kind: str, entities: list[EntityState],
                  emit: bool = True, remove_missing: bool = False) -> dict:
        """Upsert the given entities as the new truth for (source, kind).
        Emits <source>.entity_added / .entity_updated; optionally removes
        entities that disappeared. Returns a delta summary."""
        prev = {e.entity_id: e for e in self.state.list_entities(kind=kind, source=source, limit=10000)}
        added = updated = unchanged = removed = 0
        seen = set()
        for ent in entities:
            ent.source, ent.kind = source, kind
            seen.add(ent.entity_id)
            old = prev.get(ent.entity_id)
            self.state.upsert_entity(ent)
            if old is None:
                added += 1
                if emit:
                    self.events.publish(f"{source}.entity_added",
                                        {"entity_id": ent.entity_id, "kind": kind,
                                         "title": ent.title}, source=source)
            elif old.state != ent.state or old.title != ent.title:
                updated += 1
                if emit:
                    self.events.publish(f"{source}.entity_updated",
                                        {"entity_id": ent.entity_id, "kind": kind,
                                         "title": ent.title}, source=source)
            else:
                unchanged += 1
        if remove_missing:
            for eid, old in prev.items():
                if eid not in seen:
                    self.state.delete_entity(eid)
                    removed += 1
                    if emit:
                        self.events.publish(f"{source}.entity_removed",
                                            {"entity_id": eid, "kind": kind}, source=source)
        if self.connectors is not None:
            try:
                self.connectors.state.set_connector_state(source, last_sync=_now(), health="ok")
            except Exception:
                pass
        return {"source": source, "kind": kind, "added": added, "updated": updated,
                "unchanged": unchanged, "removed": removed, "total": len(entities)}

    # --- pull from a connector kind ------------------------------------
    def sync_connector(self, kind: str, mode: str = "private", limit: int = 100) -> dict:
        """Pull items from a registry connector and reconcile them as entities."""
        if self.connectors is None or self.connectors.registry is None:
            return {"error": "no connector registry"}
        items = self.connectors.registry.list(kind, mode=mode, limit=limit)
        ents = [EntityState(entity_id=f"{kind}:{it.get('id') or it.get('title')}",
                            kind=kind, source=kind, title=it.get("title", ""),
                            state={k: v for k, v in it.items() if k != "body"})
                for it in items]
        return self.reconcile(kind, kind, ents, emit=True)
