"""Digital twin layer — a single composed, queryable model of the user.

    from amy.twin import DigitalTwin
    twin = DigitalTwin(notes, collab_db, llm=None)
    twin.snapshot()              # {profile, memory, goals, traits}
    twin.ask("what am I focused on?")
"""
from .twin import DigitalTwin
