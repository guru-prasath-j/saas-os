"""Digital Twin package — the user's evolving model of self.

  * DigitalTwinEngine — composes vault, memory, goals, habits, decisions
  * PersonalityEngine — learns style, preferences, priorities, decision patterns
  * FutureSelfAgent   — validates decisions against long-term goals

The Digital Twin is intended to become the primary interface to PIOS over time:
every other engine feeds it, and it speaks for the user.
"""
from .digital_twin_engine import DigitalTwinEngine
from .personality_engine import PersonalityEngine
from .future_self_agent import FutureSelfAgent
