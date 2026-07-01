"""Connector abstraction for external personal data (email, calendar, tasks).

All connectors are PRIVATE-ONLY: they may only be read in private mode, never in
the public portfolio. Real providers (Gmail, Google Calendar, Google Tasks, etc.)
implement this same interface — swap the local provider for an API-backed one
without changing callers.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Item:
    kind: str            # email | calendar | tasks
    id: str
    title: str
    body: str = ""
    ts: str = ""         # ISO timestamp / date / due date
    meta: dict = field(default_factory=dict)


class Connector:
    kind = "base"
    private_only = True   # never exposed in public portfolio mode

    def list(self, limit: int = 50) -> list[Item]:
        raise NotImplementedError
