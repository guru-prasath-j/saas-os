"""Collaboration store (collab.db) — memory, agent cards, goals/milestones, activity log.

Plain sqlite3 so it's a single inspectable file per user. Independent of the
knowledge/metadata DBs.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def _conn(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    return c


class CollabDB:
    def __init__(self, db_path):
        self.conn = _conn(Path(db_path))
        self._init()

    def _init(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS prefs (key TEXT PRIMARY KEY, value TEXT);

            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, text TEXT);

            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT,
                detail TEXT, domain TEXT);

            CREATE TABLE IF NOT EXISTS note_access (
                path TEXT PRIMARY KEY, count INTEGER DEFAULT 0, last_ts TEXT);

            CREATE TABLE IF NOT EXISTS agent_cards (
                agent TEXT PRIMARY KEY, topics TEXT, faqs TEXT,
                last_files TEXT, importance REAL, updated_at TEXT);

            CREATE TABLE IF NOT EXISTS goals (
                id TEXT PRIMARY KEY, title TEXT, domain TEXT, status TEXT,
                progress REAL DEFAULT 0, created_at TEXT, target_date TEXT);

            CREATE TABLE IF NOT EXISTS milestones (
                id TEXT PRIMARY KEY, goal_id TEXT, title TEXT,
                done INTEGER DEFAULT 0, position INTEGER DEFAULT 0);

            CREATE TABLE IF NOT EXISTS agent_state (
                agent TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1);

            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, ts TEXT, type TEXT, payload TEXT, source TEXT);

            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, goal_id TEXT, title TEXT, done INTEGER DEFAULT 0, created_at TEXT);

            CREATE TABLE IF NOT EXISTS goal_deps (
                goal_id TEXT, depends_on TEXT, UNIQUE(goal_id, depends_on));

            CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY, ts TEXT, title TEXT, reason TEXT, domain TEXT,
                confidence REAL, outcome TEXT, status TEXT);

            -- Operational Layer: live entity snapshots + connector state
            CREATE TABLE IF NOT EXISTS op_entities (
                entity_id TEXT PRIMARY KEY, kind TEXT, source TEXT, title TEXT,
                state TEXT, updated_at TEXT);

            CREATE TABLE IF NOT EXISTS op_connector_state (
                connector TEXT PRIMARY KEY, status TEXT, health TEXT,
                last_sync TEXT, cursor TEXT, detail TEXT);

            CREATE TABLE IF NOT EXISTS notifications (
                id           TEXT PRIMARY KEY,
                type         TEXT NOT NULL,
                title        TEXT NOT NULL,
                body         TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                read_at      TEXT,
                priority     TEXT DEFAULT 'normal',
                related_entity TEXT DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_act_ts       ON activities(ts);
            CREATE INDEX IF NOT EXISTS idx_op_ent_kind  ON op_entities(kind);
            CREATE INDEX IF NOT EXISTS idx_evt_type     ON events(type);
            CREATE INDEX IF NOT EXISTS idx_ms_goal      ON milestones(goal_id);
            CREATE INDEX IF NOT EXISTS idx_notif_read   ON notifications(read_at);
            CREATE INDEX IF NOT EXISTS idx_notif_ts     ON notifications(created_at);
            """
        )
        self.conn.commit()
        self._migrate()

    def _migrate(self):
        """Idempotent schema upgrades added after initial release."""
        # finance_meta on goals — stores savings target for drift analysis
        try:
            self.conn.execute("ALTER TABLE goals ADD COLUMN finance_meta TEXT DEFAULT '{}'")
            self.conn.commit()
        except Exception:
            pass   # column already exists

    def reset(self):
        for t in ("prefs", "summaries", "activities", "note_access", "agent_cards",
                  "goals", "milestones", "agent_state", "events", "tasks", "goal_deps", "decisions",
                  "op_entities", "op_connector_state", "notifications"):
            self.conn.execute(f"DELETE FROM {t}")
        self.conn.commit()

    def close(self):
        self.conn.close()
