"""ConnectorManager (OL-3) — lifecycle + health over the existing registry.

Wraps ``amy.connectors.ConnectorRegistry`` (pull-based providers) with uniform
lifecycle (register / start / stop / status) and health monitoring, persisting
state to ``op_connector_state`` via the StateStore. It does NOT replace the
registry or the providers — it manages them.

Health is determined by attempting a cheap read against each connector and
recording the outcome, so the Operational Layer can surface failing connectors
without each agent probing them individually.
"""
from __future__ import annotations

import datetime as _dt

from ..connectors import ConnectorRegistry
from .state import StateStore


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class ConnectorManager:
    def __init__(self, state_store: StateStore, connector_dir=None, registry=None):
        self.state = state_store
        self.registry = registry or (ConnectorRegistry(connector_dir) if connector_dir else None)

    # --- lifecycle ------------------------------------------------------
    def register(self, kind: str, provider=None) -> dict:
        if provider is not None and self.registry is not None:
            self.registry.register(kind, provider)
        return self.state.set_connector_state(kind, status="registered", health="unknown")

    def start(self, kind: str) -> dict:
        return self.state.set_connector_state(kind, status="running")

    def stop(self, kind: str) -> dict:
        return self.state.set_connector_state(kind, status="stopped")

    def status(self, kind: str | None = None):
        if kind:
            return self.state.get_connector_state(kind)
        return self.state.all_connector_states()

    # --- health ---------------------------------------------------------
    def check_health(self, kind: str, mode: str = "private") -> dict:
        """Probe one connector with a cheap read; record ok/degraded + detail."""
        if self.registry is None or kind not in self.registry.kinds():
            return self.state.set_connector_state(kind, health="unknown",
                                                  detail="no provider")
        try:
            self.registry.list(kind, mode=mode, limit=1)
            return self.state.set_connector_state(
                kind, health="ok", last_sync=_now(), detail=None)
        except PermissionError as e:
            return self.state.set_connector_state(kind, health="blocked", detail=str(e))
        except Exception as e:
            return self.state.set_connector_state(kind, health="degraded", detail=str(e))

    def check_all(self, mode: str = "private") -> list[dict]:
        if self.registry is None:
            return self.state.all_connector_states()
        return [self.check_health(k, mode=mode) for k in self.registry.kinds()]

    def kinds(self) -> list[str]:
        return self.registry.kinds() if self.registry else []
