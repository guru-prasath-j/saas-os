"""Planner Agent — goals, milestones, action plans, progress tracking.

Also acts as a participant in multi-agent execution: `plan(query, context)` turns
a "can I / should I / how do I" query into an action plan section to be merged.
"""
from __future__ import annotations

import datetime as _dt
import re
import uuid


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class PlannerAgent:
    name = "planner"

    def __init__(self, db, llm=None, events=None):
        self.db = db.conn
        self.llm = llm
        self.events = events   # optional EventStore for goal.* events

    def _emit(self, etype, payload):
        if self.events is not None:
            try:
                self.events.emit(etype, payload, source="planner")
            except Exception:
                pass

    # --- goals & milestones -------------------------------------------------
    def create_goal(self, title: str, domain: str = "general", target_date: str | None = None) -> str:
        gid = _id()
        self.db.execute(
            "INSERT INTO goals (id, title, domain, status, progress, created_at, target_date) "
            "VALUES (?,?,?,?,?,?,?)", (gid, title, domain, "active", 0.0, _now(), target_date))
        self.db.commit()
        self._emit("goal.created", {"id": gid, "title": title, "domain": domain})
        return gid

    def add_milestone(self, goal_id: str, title: str) -> str:
        mid = _id()
        pos = self.db.execute("SELECT COUNT(*) c FROM milestones WHERE goal_id=?", (goal_id,)).fetchone()["c"]
        self.db.execute(
            "INSERT INTO milestones (id, goal_id, title, done, position) VALUES (?,?,?,0,?)",
            (mid, goal_id, title, pos))
        self.db.commit()
        self._recompute(goal_id)
        return mid

    def complete_milestone(self, milestone_id: str, done: bool = True):
        row = self.db.execute("SELECT goal_id FROM milestones WHERE id=?", (milestone_id,)).fetchone()
        self.db.execute("UPDATE milestones SET done=? WHERE id=?", (1 if done else 0, milestone_id))
        self.db.commit()
        if row:
            self._recompute(row["goal_id"])

    def update_goal(self, goal_id: str, title: str | None = None,
                    status: str | None = None,
                    target_date: str | None = None) -> bool:
        """Human-facing edit of the user's own goal — direct write, same
        stance as the career profile PUT."""
        sets, args = [], []
        if title is not None and title.strip():
            sets.append("title=?"); args.append(title.strip())
        if status is not None and status in ("active", "done", "paused", "archived"):
            sets.append("status=?"); args.append(status)
        if target_date is not None:
            sets.append("target_date=?"); args.append(target_date or None)
        if not sets:
            return False
        args.append(goal_id)
        c = self.db.execute(f"UPDATE goals SET {', '.join(sets)} WHERE id=?", args)
        self.db.commit()
        return c.rowcount > 0

    def delete_goal(self, goal_id: str) -> bool:
        """Delete a goal with its milestones/tasks; goal-linked learning
        focuses are unlinked (kept), not deleted — the focus is the user's
        own reading interest, the goal was just an annotation on it."""
        if self.db.execute("SELECT id FROM goals WHERE id=?", (goal_id,)).fetchone() is None:
            return False
        self.db.execute("DELETE FROM milestones WHERE goal_id=?", (goal_id,))
        try:
            self.db.execute("DELETE FROM tasks WHERE goal_id=?", (goal_id,))
        except Exception:
            pass
        try:
            # lazily-created table (AutomationStore) — may not exist yet
            self.db.execute("UPDATE learning_focuses SET goal_id=NULL WHERE goal_id=?",
                            (goal_id,))
        except Exception:
            pass
        self.db.execute("DELETE FROM goals WHERE id=?", (goal_id,))
        self.db.commit()
        return True

    def update_milestone(self, milestone_id: str, title: str) -> bool:
        if not (title or "").strip():
            return False
        c = self.db.execute("UPDATE milestones SET title=? WHERE id=?",
                            (title.strip(), milestone_id))
        self.db.commit()
        return c.rowcount > 0

    def delete_milestone(self, milestone_id: str) -> bool:
        row = self.db.execute("SELECT goal_id FROM milestones WHERE id=?",
                              (milestone_id,)).fetchone()
        if row is None:
            return False
        self.db.execute("DELETE FROM milestones WHERE id=?", (milestone_id,))
        self.db.commit()
        # _recompute no-ops on zero milestones — reset progress explicitly
        # so deleting the last one doesn't freeze a stale percentage
        if self.db.execute("SELECT COUNT(*) c FROM milestones WHERE goal_id=?",
                           (row["goal_id"],)).fetchone()["c"] == 0:
            self.db.execute("UPDATE goals SET progress=0, status='active' WHERE id=?",
                            (row["goal_id"],))
            self.db.commit()
        else:
            self._recompute(row["goal_id"])
        return True

    def _recompute(self, goal_id: str):
        rs = self.db.execute("SELECT done FROM milestones WHERE goal_id=?", (goal_id,)).fetchall()
        if not rs:
            return
        prog = round(100.0 * sum(r["done"] for r in rs) / len(rs), 1)
        status = "done" if prog >= 100 else "active"
        was = self.db.execute("SELECT status, title FROM goals WHERE id=?", (goal_id,)).fetchone()
        self.db.execute("UPDATE goals SET progress=?, status=? WHERE id=?", (prog, status, goal_id))
        self.db.commit()
        if status == "done" and was and was["status"] != "done":
            self._emit("goal.completed", {"id": goal_id, "title": was["title"]})

    def set_progress(self, goal_id: str, progress: float):
        self.db.execute("UPDATE goals SET progress=?, status=? WHERE id=?",
                        (progress, "done" if progress >= 100 else "active", goal_id))
        self.db.commit()

    def get_plan(self, goal_id: str) -> dict | None:
        g = self.db.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        if not g:
            return None
        ms = self.db.execute(
            "SELECT id, title, done, position FROM milestones WHERE goal_id=? ORDER BY position",
            (goal_id,)).fetchall()
        return {**dict(g), "milestones": [dict(m) for m in ms]}

    def list_goals(self) -> list[dict]:
        gs = self.db.execute("SELECT * FROM goals ORDER BY created_at DESC").fetchall()
        return [self.get_plan(g["id"]) for g in gs]

    # --- finance target linking for drift analysis -------------------------

    def set_finance_target(self, goal_id: str, savings_target: float,
                           monthly_savings_category: str = "Savings") -> bool:
        """
        Attach a finance savings target to a goal.
        Stored as JSON in the finance_meta column.
        E.g. set_finance_target(gid, 100000, "Savings") means:
          "I want to save ₹1L, tracked via transactions categorised 'Savings'."
        """
        import json
        meta = json.dumps({
            "savings_target": savings_target,
            "monthly_savings_category": monthly_savings_category,
        })
        c = self.db.execute(
            "UPDATE goals SET finance_meta=? WHERE id=?", (meta, goal_id))
        self.db.commit()
        return c.rowcount > 0

    def get_finance_target(self, goal_id: str) -> dict | None:
        """Return the finance target dict for a goal, or None if not set."""
        import json
        row = self.db.execute(
            "SELECT finance_meta FROM goals WHERE id=?", (goal_id,)).fetchone()
        if not row:
            return None
        raw = row["finance_meta"] or "{}"
        try:
            data = json.loads(raw)
        except Exception:
            return None
        return data if data.get("savings_target") else None

    # --- multi-agent participant -------------------------------------------
    _PLAN_CUES = ("plan", "afford", "should i", "can i", "how do i", "how can i",
                  "while", "goal", "save for", "switch", "roadmap", "steps to")

    @classmethod
    def wants_plan(cls, query: str) -> bool:
        q = query.lower()
        return any(c in q for c in cls._PLAN_CUES)

    def plan(self, query: str, context: str = "") -> dict:
        """Produce an action-plan section for the merge (no DB writes)."""
        if self.llm is not None:
            text, model = self.llm.generate(
                "You are a planning agent. Given the question and context, propose a short, "
                "numbered action plan with concrete milestones.", query, context)
            steps = [s.strip(" -") for s in re.split(r"\n+", text) if s.strip()]
        else:
            model = "none"
            steps = [
                "Clarify the goal and a target date.",
                "List what each domain (e.g. finance, career) requires.",
                "Break it into 3-5 milestones with rough timing.",
                "Track progress and review weekly.",
            ]
            text = "Suggested action plan:\n" + "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps))
        return {"domain": "planner", "answer": text, "model": model,
                "steps": steps, "sources": []}
