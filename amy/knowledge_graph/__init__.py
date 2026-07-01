"""Global Knowledge Graph — cross-source entity graph.

Connects notes, emails, calendar events, tasks, goals and memories with typed
relationships (depends_on, related_to, supports, blocks, belongs_to), with
automatic generation, querying, and traversal.
"""
from .store import GraphStore, NODE_TYPES, REL_TYPES
from .builder import build_graph
