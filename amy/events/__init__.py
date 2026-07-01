"""Event layer: persistent event store + in-process bus + triggers.

    from amy.events import EventStore, register_default_triggers
    ev = EventStore(collab_db)
    ev.subscribe("goal.completed", handler)
    ev.emit("query.asked", {"query": "..."}, source="chat")
"""
from .store import (EventStore, QUERY_ASKED, GOAL_CREATED, GOAL_COMPLETED,
                    CAPTURE_ADDED, VAULT_IMPORTED, AGENT_TOGGLED, DIGEST_GENERATED)
from .triggers import register_default_triggers, build_digest
