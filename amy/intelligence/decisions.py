"""Decision Engine (PIOS intelligence layer) — a decision journal.

Stores decisions with reason, confidence, and (later) outcome, so they can be
reviewed. Optional event emission. Additive; no existing module changed.
"""
from __future__ import annotations

import datetime as _dt
import uuid


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class DecisionEngine:
    def __init__(self, collab_db, events=None):
        self.db = collab_db.conn
        self.events = events

    def record(self, title: str, reason: str = "", domain: str = "general",
               confidence: float | None = None) -> str:
        did = _id()
        self.db.execute(
            "INSERT INTO decisions (id, ts, title, reason, domain, confidence, outcome, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (did, _now(), title, reason, domain, confidence, None, "open"))
        self.db.commit()
        if self.events is not None:
            try:
                self.events.emit("decision.recorded", {"id": did, "title": title}, source="decision")
            except Exception:
                pass
        return did

    def set_outcome(self, decision_id: str, outcome: str, status: str = "resolved"):
        self.db.execute("UPDATE decisions SET outcome=?, status=? WHERE id=?",
                        (outcome, status, decision_id))
        self.db.commit()
        if self.events is not None:
            try:
                self.events.emit("decision.resolved", {"id": decision_id, "status": status}, source="decision")
            except Exception:
                pass

    def get(self, decision_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
        return dict(r) if r else None

    def list(self, limit: int = 100) -> list[dict]:
        rs = self.db.execute("SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rs]
