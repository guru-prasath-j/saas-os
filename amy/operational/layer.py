"""OperationalLayer (OL-5) — the single façade agents use.

Composes the pieces (state, sensors, connectors, sync, replay) over the ONE
existing event bus and the ONE per-user collab.db. Agents and APIs talk to this
object instead of importing events/, sensors/, and connectors/ separately.

    ops = OperationalLayer(collab_db, event_store, connector_dir=...)
    ops.connectors.check_all()
    ops.entities.list_entities(source="github")
    ops.publish("github.NEW_COMMIT", {...})
    ops.subscribe("github.NEW_COMMIT", handler)
    ops.replay(handler, types=[...])

It creates no new bus and no new database — everything is wired to what exists.
"""
from __future__ import annotations

from .state import StateStore
from .sensors import SensorRegistry
from .connectors import ConnectorManager
from .sync import SyncService
from .replay import ReplayService


class OperationalLayer:
    def __init__(self, collab_db, event_store, connector_dir=None):
        self.db = collab_db
        self.events = event_store                       # the ONE bus (EventStore)
        self.state = StateStore(collab_db)              # entity + connector state
        self.entities = self.state                      # entity-facing alias
        self.sensors = SensorRegistry()
        self.connectors = ConnectorManager(self.state, connector_dir=connector_dir)
        self.sync = SyncService(self.state, event_store, connector_manager=self.connectors)
        self.replay_service = ReplayService(collab_db)

    # --- bus passthrough (one bus) -------------------------------------
    def publish(self, event_type: str, payload: dict | None = None, source: str = "") -> str:
        return self.events.publish(event_type, payload or {}, source=source)

    def subscribe(self, event_type: str, handler):
        return self.events.subscribe(event_type, handler)

    def replay(self, handler, since_ts=None, types=None, limit=1000):
        return self.replay_service.replay(handler, since_ts=since_ts, types=types, limit=limit)

    # --- registration helpers ------------------------------------------
    def register_sensor(self, sensor):
        self.sensors.register(sensor)
        # reflect in connector state so health/status views include sensors
        self.state.set_connector_state(getattr(sensor, "name", "sensor"),
                                       status="running", health="ok")
        return sensor

    def register_default_sensors(self):
        """Register the built-in sensors (currently GitHub). Safe/idempotent."""
        try:
            from ..sensors import GitHubSensor
            self.register_sensor(GitHubSensor(self.events))
        except Exception:
            pass
        return self.sensors.names()

    # --- snapshot for APIs / dashboards --------------------------------
    def snapshot(self) -> dict:
        return {
            "connectors": self.connectors.status(),
            "sensors": self.sensors.names(),
            "entity_count": self.state.count_entities(),
            "event_types": self.events.stats(),
        }
