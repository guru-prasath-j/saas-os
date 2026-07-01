"""Universal Search — one ranked search across every source.

Thin aggregator over existing services (hybrid retrieval for the vault, the
connector registry for email/calendar/tasks, collab.db for memories/goals).
No new index or retrieval implementation.
"""
from .universal import UniversalSearch
