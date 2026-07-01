"""Habit Tracker — daily check-ins, streaks, and analytics."""
from __future__ import annotations

import datetime as _dt
import sqlite3
import uuid
from pathlib import Path


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _today() -> str:
    return _dt.date.today().isoformat()


class HabitEngine:
    def __init__(self, db_path):
        self.db = sqlite3.connect(str(db_path))
        self.db.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS habits (
                id TEXT PRIMARY KEY, title TEXT, frequency TEXT DEFAULT 'daily',
                color TEXT DEFAULT '#22D3EE', created_at TEXT, archived INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS habit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                habit_id TEXT, date TEXT, done INTEGER DEFAULT 1, note TEXT DEFAULT '',
                UNIQUE(habit_id, date)
            );
        """)
        self.db.commit()

    # --- writes ---------------------------------------------------------
    def add(self, title: str, frequency: str = "daily", color: str = "#22D3EE") -> str:
        hid = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO habits (id,title,frequency,color,created_at) VALUES (?,?,?,?,?)",
            (hid, title, frequency, color, _now()),
        )
        self.db.commit()
        return hid

    def check_in(self, habit_id: str, date: str | None = None,
                 done: bool = True, note: str = "") -> dict:
        date = date or _today()
        self.db.execute(
            "INSERT INTO habit_logs (habit_id,date,done,note) VALUES (?,?,?,?) "
            "ON CONFLICT(habit_id,date) DO UPDATE SET done=excluded.done, note=excluded.note",
            (habit_id, date, int(done), note),
        )
        self.db.commit()
        return {"habit_id": habit_id, "date": date, "done": done}

    def archive(self, habit_id: str) -> None:
        self.db.execute("UPDATE habits SET archived=1 WHERE id=?", (habit_id,))
        self.db.commit()

    # --- reads ----------------------------------------------------------
    def list_habits(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM habits WHERE archived=0 ORDER BY created_at"
        ).fetchall()
        today = _today()
        result = []
        for r in rows:
            checked = bool(self.db.execute(
                "SELECT 1 FROM habit_logs WHERE habit_id=? AND date=? AND done=1",
                (r["id"], today),
            ).fetchone())
            result.append({
                "id": r["id"], "title": r["title"],
                "frequency": r["frequency"], "color": r["color"],
                "checked_today": checked, "streak": self._streak(r["id"]),
            })
        return result

    def heatmap(self, habit_id: str, days: int = 90) -> list[dict]:
        since = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
        rows = self.db.execute(
            "SELECT date, done FROM habit_logs WHERE habit_id=? AND date>=? ORDER BY date",
            (habit_id, since),
        ).fetchall()
        return [{"date": r["date"], "done": bool(r["done"])} for r in rows]

    # --- internals ------------------------------------------------------
    def _streak(self, habit_id: str) -> int:
        rows = self.db.execute(
            "SELECT date FROM habit_logs WHERE habit_id=? AND done=1 "
            "ORDER BY date DESC LIMIT 365",
            (habit_id,),
        ).fetchall()
        if not rows:
            return 0
        dates = [_dt.date.fromisoformat(r["date"]) for r in rows]
        streak, cur = 0, _dt.date.today()
        for d in dates:
            if d >= cur - _dt.timedelta(days=1):
                streak += 1
                cur = d
            else:
                break
        return streak

    def close(self):
        self.db.close()
