"""Operational maintenance tick (OL-6).

A cheap, safe periodic job the existing background worker can call: probe every
connector's health and record it in ``op_connector_state``. It reuses the one
collab.db and the one bus; it never starts a second scheduler.

Kept side-effect-light on purpose — health probing only. Full per-connector
sync (which emits delta events) is exposed via the API/manual path so the
background loop stays predictable.
"""
from __future__ import annotations


def run_ops_maintenance(collab_db, connector_dir=None) -> dict:
    """Probe connector health for one user. Returns the health report."""
    from .layer import OperationalLayer
    from ..events import EventStore
    ops = OperationalLayer(collab_db, EventStore(collab_db), connector_dir=connector_dir)
    try:
        return {"health": ops.connectors.check_all(mode="private")}
    except Exception as e:  # maintenance must never crash the scheduler
        return {"error": str(e)}
