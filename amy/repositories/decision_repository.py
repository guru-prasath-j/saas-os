"""DecisionRepository — data access for decisions.

Reuses the existing `decisions` table (columns: id, ts, title, reason, domain,
confidence, outcome, status). The `domain` column stores the category, so this
repository is fully interoperable with the original
``amy.intelligence.decisions.DecisionEngine``.
"""
from __future__ import annotations

from ..models.decision_model import Decision, CATEGORIES


class DecisionRepository:
    def __init__(self, collab_db):
        self.db = collab_db.conn

    # --- writes ---------------------------------------------------------
    def add(self, decision: Decision) -> str:
        self.db.execute(
            "INSERT INTO decisions (id, ts, title, reason, domain, confidence, outcome, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (decision.id, decision.ts, decision.title, decision.reason,
             decision.category, decision.confidence, decision.outcome, decision.status))
        self.db.commit()
        return decision.id

    def set_outcome(self, decision_id: str, outcome: str, status: str = "resolved") -> None:
        self.db.execute("UPDATE decisions SET outcome=?, status=? WHERE id=?",
                        (outcome, status, decision_id))
        self.db.commit()

    # --- reads ----------------------------------------------------------
    @staticmethod
    def _row(r) -> Decision:
        return Decision(id=r["id"], title=r["title"], category=r["domain"] or "personal",
                        reason=r["reason"] or "", outcome=r["outcome"],
                        confidence=r["confidence"], status=r["status"] or "open",
                        ts=r["ts"] or "")

    def get(self, decision_id: str) -> Decision | None:
        r = self.db.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
        return self._row(r) if r else None

    def all(self, category: str | None = None, limit: int = 500) -> list[Decision]:
        if category and category in CATEGORIES:
            rs = self.db.execute(
                "SELECT * FROM decisions WHERE domain=? ORDER BY ts DESC LIMIT ?",
                (category, limit)).fetchall()
        else:
            rs = self.db.execute(
                "SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rs]
