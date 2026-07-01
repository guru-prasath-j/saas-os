"""Decision model.

Persisted in the existing `decisions` table (the `domain` column holds the
category, for backward compatibility with the original decision store).
"""
from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import dataclass

CATEGORIES = ["career", "finance", "health", "learning", "projects", "personal"]


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


@dataclass
class Decision:
    id: str
    title: str
    category: str = "personal"
    reason: str = ""
    outcome: str | None = None
    confidence: float | None = None
    status: str = "open"
    ts: str = ""

    @staticmethod
    def new(title: str, category: str = "personal", reason: str = "",
            confidence: float | None = None) -> "Decision":
        return Decision(
            id=uuid.uuid4().hex[:12], title=title,
            category=category if category in CATEGORIES else "personal",
            reason=reason, confidence=confidence, status="open", ts=_now())

    def to_dict(self) -> dict:
        return {"decision_id": self.id, "title": self.title, "category": self.category,
                "reason": self.reason, "outcome": self.outcome, "confidence": self.confidence,
                "status": self.status, "timestamp": self.ts}
