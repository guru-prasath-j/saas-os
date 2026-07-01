"""Multi-agent collaboration layer: Memory Manager, Planner, Agent Cards,
Reflection and Learning agents, orchestrated by CollabMaster.

    from amy.collab import CollabMaster
    cm = CollabMaster(notes, "/data/collab.db", llm=None)
    cm.handle("Can I afford a Europe trip while switching careers?")
"""
from .db import CollabDB
from .memory import MemoryManager
from .cards import AgentCards
from .planner import PlannerAgent
from .reflection import ReflectionAgent
from .learning import LearningAgent
from .orchestrator import CollabMaster
