"""Operational Layer — the single source of live operational state.

Built on top of existing infrastructure (one Event Bus = EventStore, one DB file
= collab.db, the existing ConnectorRegistry and GitHubSensor). It adds the
missing *live-state half*: an entity registry, connector lifecycle/health,
state synchronization, and event replay — behind one façade agents can use.

Invariants:
  * exactly ONE event bus (amy.events.EventStore) — never a second
  * exactly ONE per-user DB file (collab.db) — operational tables live there
  * connectors stay decoupled from memory and domain agents

Build order (see docs/operational_layer_analysis.md): OL-1 models+state shipped
here; later modules add sensors, connectors, sync, replay, façade.
"""
from .models import OperationalEvent, EntityState
from .state import StateStore
from .sensors import Sensor, SensorRegistry
from .connectors import ConnectorManager
from .sync import SyncService
from .replay import ReplayService
from .layer import OperationalLayer
from .agent import OperationalAgent, CareerOpsAgent
