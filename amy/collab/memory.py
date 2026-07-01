"""Memory Manager — user preferences, conversation summaries, recent activities,
and frequently accessed notes.
"""
from __future__ import annotations

import datetime as _dt


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class MemoryManager:
    def __init__(self, db):
        self.db = db.conn

    # --- preferences --------------------------------------------------------
    def set_pref(self, key: str, value: str):
        self.db.execute("INSERT OR REPLACE INTO prefs (key, value) VALUES (?,?)", (key, value))
        self.db.commit()

    def get_pref(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM prefs WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def get_prefs(self) -> dict:
        return {r["key"]: r["value"] for r in self.db.execute("SELECT key, value FROM prefs")}

    # --- conversation summaries --------------------------------------------
    def add_summary(self, text: str):
        self.db.execute("INSERT INTO summaries (ts, text) VALUES (?,?)", (_now(), text))
        self.db.commit()

    def recent_summaries(self, n: int = 5) -> list[dict]:
        rs = self.db.execute("SELECT ts, text FROM summaries ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [{"ts": r["ts"], "text": r["text"]} for r in rs]

    # --- activity log -------------------------------------------------------
    def log_activity(self, kind: str, detail: str, domain: str | None = None):
        self.db.execute("INSERT INTO activities (ts, kind, detail, domain) VALUES (?,?,?,?)",
                        (_now(), kind, detail, domain))
        self.db.commit()

    def recent_activities(self, n: int = 20) -> list[dict]:
        rs = self.db.execute(
            "SELECT ts, kind, detail, domain FROM activities ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rs]

    # --- frequently accessed notes -----------------------------------------
    def record_access(self, path: str):
        self.db.execute(
            "INSERT INTO note_access (path, count, last_ts) VALUES (?,1,?) "
            "ON CONFLICT(path) DO UPDATE SET count=count+1, last_ts=excluded.last_ts",
            (path, _now()))
        self.db.commit()

    def frequent_notes(self, n: int = 10) -> list[dict]:
        rs = self.db.execute(
            "SELECT path, count, last_ts FROM note_access ORDER BY count DESC, last_ts DESC LIMIT ?",
            (n,)).fetchall()
        return [dict(r) for r in rs]

    # --- conversation turns (used as context for follow-ups) ---------------
    def add_turn(self, query: str, answer: str):
        self.add_summary(f"User: {query}\nAmy: {answer[:600]}")

    def conversation_context(self, n: int = 3) -> str:
        """Recent turns + preferences, formatted to prepend to agent context so
        Amy 'remembers' the conversation and the user's stated preferences."""
        parts = []
        prefs = self.get_prefs()
        if prefs:
            parts.append("Preferences: " + ", ".join(f"{k}={v}" for k, v in prefs.items()))
        turns = list(reversed(self.recent_summaries(n)))  # oldest -> newest
        if turns:
            parts.append("\n".join(t["text"] for t in turns))
        return "\n\n".join(parts).strip()

    # --- snapshot used by the master to prime context ----------------------
    def snapshot(self) -> dict:
        return {
            "preferences": self.get_prefs(),
            "recent_summaries": self.recent_summaries(3),
            "recent_activities": self.recent_activities(10),
            "frequent_notes": self.frequent_notes(5),
        }
