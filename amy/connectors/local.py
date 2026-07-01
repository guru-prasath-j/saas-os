"""Local file-backed connector providers (real + offline-testable).

Reads JSON arrays from the user's connector data dir:
    email.json, calendar.json, tasks.json
Each item is a dict; common field aliases are normalized. Missing file -> [].

These prove the architecture end to end without OAuth. A Gmail/Google provider
subclasses Connector with the same `kind`/`list()` and is dropped into the registry.
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import Connector, Item


class _LocalJSON(Connector):
    def __init__(self, data_dir, filename: str, kind: str):
        self.path = Path(data_dir) / filename
        self.kind = kind

    def list(self, limit: int = 50) -> list[Item]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return []
        items = []
        for d in (data or [])[:limit]:
            items.append(Item(
                kind=self.kind,
                id=str(d.get("id", "")),
                title=d.get("title") or d.get("subject") or d.get("summary") or "",
                body=d.get("body") or d.get("description") or d.get("snippet") or "",
                ts=d.get("ts") or d.get("date") or d.get("due") or d.get("start") or "",
                meta=d,
            ))
        return items


class LocalEmailProvider(_LocalJSON):
    def __init__(self, data_dir):
        super().__init__(data_dir, "email.json", "email")


class LocalCalendarProvider(_LocalJSON):
    def __init__(self, data_dir):
        super().__init__(data_dir, "calendar.json", "calendar")


class LocalTaskProvider(_LocalJSON):
    def __init__(self, data_dir):
        super().__init__(data_dir, "tasks.json", "tasks")
