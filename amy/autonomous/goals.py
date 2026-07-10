"""Goal Engine (PIOS v2) — goals, milestones, tasks, dependencies, progress.

Backward-compatible: goals/milestones are delegated to the existing PlannerAgent
(unchanged). This adds tasks, dependencies (with a cycle guard + blocked
detection), and a combined progress across milestones + tasks.
"""
from __future__ import annotations

import datetime as _dt
import uuid


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class GoalEngine:
    def __init__(self, collab_db, events=None):
        self.db = collab_db.conn
        self.events = events
        from ..collab.planner import PlannerAgent
        self.planner = PlannerAgent(collab_db, events=events)

    # --- goals & milestones (delegate to existing planner) -----------------
    def create_goal(self, title, domain="general", target_date=None):
        return self.planner.create_goal(title, domain, target_date)

    def add_milestone(self, goal_id, title):
        return self.planner.add_milestone(goal_id, title)

    def complete_milestone(self, milestone_id, done=True):
        return self.planner.complete_milestone(milestone_id, done)

    def list_goals(self):
        return self.planner.list_goals()

    # --- tasks --------------------------------------------------------------
    def add_task(self, goal_id, title) -> str:
        tid = _id()
        self.db.execute("INSERT INTO tasks (id, goal_id, title, done, created_at) VALUES (?,?,?,0,?)",
                        (tid, goal_id, title, _now()))
        self.db.commit()
        self._recompute(goal_id)
        return tid

    def complete_task(self, task_id, done=True):
        row = self.db.execute("SELECT goal_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.db.execute("UPDATE tasks SET done=? WHERE id=?", (1 if done else 0, task_id))
        self.db.commit()
        if row and row["goal_id"]:
            self._recompute(row["goal_id"])

    def _recompute(self, goal_id) -> None:
        """Persist the combined milestones+tasks ratio to goals.progress —
        mirrors PlannerAgent._recompute (milestones-only), which this class
        otherwise doesn't touch. Without this, a task added/completed via
        GoalEngine (career-plan weekly tasks, the learning-feedback loop)
        never moves the progress number every UI surface actually reads,
        even though self.progress() already computes it correctly on
        demand. No-ops for the blank/legacy goal_id some non-goal task
        proposals use (e.g. errand place-tagged reminders)."""
        if not goal_id:
            return
        g = self.db.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
        if not g:
            return
        prog = self.progress(goal_id)
        status = "done" if prog >= 100 else ("active" if g["status"] != "done" else g["status"])
        self.db.execute("UPDATE goals SET progress=?, status=? WHERE id=?",
                        (prog, status, goal_id))
        self.db.commit()

    def list_tasks(self, goal_id) -> list[dict]:
        rs = self.db.execute("SELECT id,title,done FROM tasks WHERE goal_id=?", (goal_id,)).fetchall()
        return [dict(r) for r in rs]

    # --- dependencies -------------------------------------------------------
    def add_dependency(self, goal_id, depends_on):
        if goal_id == depends_on or self._would_cycle(goal_id, depends_on):
            raise ValueError("circular dependency rejected")
        self.db.execute("INSERT OR IGNORE INTO goal_deps (goal_id, depends_on) VALUES (?,?)",
                        (goal_id, depends_on))
        self.db.commit()

    def dependencies(self, goal_id) -> list[str]:
        return [r["depends_on"] for r in
                self.db.execute("SELECT depends_on FROM goal_deps WHERE goal_id=?", (goal_id,)).fetchall()]

    def _would_cycle(self, goal_id, dep) -> bool:
        # does `dep` already depend (transitively) on `goal_id`?
        seen, stack = set(), [dep]
        while stack:
            cur = stack.pop()
            if cur == goal_id:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(self.dependencies(cur))
        return False

    def is_blocked(self, goal_id) -> bool:
        for d in self.dependencies(goal_id):
            g = self.db.execute("SELECT status FROM goals WHERE id=?", (d,)).fetchone()
            if g and g["status"] != "done":
                return True
        return False

    # --- combined progress + overview --------------------------------------
    def progress(self, goal_id) -> float:
        ms = self.db.execute("SELECT done FROM milestones WHERE goal_id=?", (goal_id,)).fetchall()
        ts = self.db.execute("SELECT done FROM tasks WHERE goal_id=?", (goal_id,)).fetchall()
        items = list(ms) + list(ts)
        if not items:
            g = self.db.execute("SELECT progress FROM goals WHERE id=?", (goal_id,)).fetchone()
            return float(g["progress"]) if g else 0.0
        return round(100.0 * sum(r["done"] for r in items) / len(items), 1)

    def overview(self) -> list[dict]:
        out = []
        for g in self.planner.list_goals():
            out.append({
                **g,
                "progress": self.progress(g["id"]),
                "blocked": self.is_blocked(g["id"]),
                "tasks": self.list_tasks(g["id"]),
                "depends_on": self.dependencies(g["id"]),
            })
        return out
