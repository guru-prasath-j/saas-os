"""Sensor base + registry (OL-2).

Generalizes the pattern already proven by ``amy.sensors.GitHubSensor``: a Sensor
authenticates to an external system, normalizes incoming data into canonical
events, and publishes them to the ONE existing Event Bus (EventStore). Agents
subscribe; connectors stay decoupled from agents and memory.

This module does NOT rewrite GitHubSensor. ``GitHubSensor`` keeps its exact API;
it simply also *is-a* ``Sensor`` (duck-typed) and can be registered here.

    External system → Sensor → Event Bus → (Memory Writer, Agents)
"""
from __future__ import annotations


class Sensor:
    """Base class for push/pull external-event sensors.

    Subclasses set ``name`` and implement ``poll()`` and/or ``ingest_webhook()``.
    All publishing goes through the injected EventStore — never a new bus.
    """
    name = "base"

    def __init__(self, event_store):
        self.events = event_store

    @property
    def authenticated(self) -> bool:  # override if the sensor needs auth
        return True

    def publish(self, event_type: str, payload: dict) -> str:
        return self.events.publish(event_type, payload, source=self.name)

    def poll(self, *args, **kwargs):
        """Pull recent external events and publish them. Override if supported."""
        return []

    def ingest_webhook(self, event_name: str, payload: dict):
        """Normalize + publish one webhook delivery. Override if supported."""
        return None


class SensorRegistry:
    """Registry of active sensors keyed by name. The Operational Layer uses it to
    route webhook deliveries and run polls uniformly."""

    def __init__(self):
        self._sensors: dict[str, object] = {}

    def register(self, sensor) -> None:
        name = getattr(sensor, "name", None) or type(sensor).__name__.lower()
        self._sensors[name] = sensor

    def get(self, name: str):
        return self._sensors.get(name)

    def names(self) -> list[str]:
        return list(self._sensors)

    def ingest_webhook(self, name: str, event_name: str, payload: dict):
        s = self._sensors.get(name)
        if s is None:
            raise KeyError(name)
        return s.ingest_webhook(event_name, payload)

    def poll(self, name: str, *args, **kwargs):
        s = self._sensors.get(name)
        if s is None:
            raise KeyError(name)
        return s.poll(*args, **kwargs)
