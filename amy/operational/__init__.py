"""Operational Layer — legacy package name; only the Sensor base class
survives here now.

The original OperationalLayer façade (entity registry, connector lifecycle/
health, state sync, event replay behind /api/ops/*) was built but never
wired to a UI, and has been removed — see CLAUDE.md's "Operational Layer"
migration checklist for the full history. What's left, ``Sensor`` /
``SensorRegistry``, is load-bearing: GmailSensor, connectors/sensors.py's
GitHubSensor/PlaneSensor, career_scout.py's JobScoutSensor, and
learning_feed/sensor.py's LearningFeedSensor all subclass ``Sensor`` from
here (``from ..operational.sensors import Sensor``). Do not delete this
module without re-pointing every one of those imports first.
"""
from .sensors import Sensor, SensorRegistry
