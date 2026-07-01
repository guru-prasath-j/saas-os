"""Three internal SQLite databases for the knowledge layer.

    metadata.db        — one row of structured metadata per note + relationships
    vector.db          — chunk text + embeddings
    agent_registry.db  — runtime domain-agent configs

Plain sqlite3 (stdlib) so each DB is a simple, inspectable file. Original markdown
is never modified — all derived data lives here.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def _conn(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    return c


class KnowledgeDBs:
    def __init__(self, data_dir):
        d = Path(data_dir)
        self.metadata = _conn(d / "metadata.db")
        self.vector = _conn(d / "vector.db")
        self.agents = _conn(d / "agent_registry.db")
        self._init()

    def _init(self):
        self.metadata.executescript(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY,
                path TEXT,
                title TEXT,
                summary TEXT,
                domain TEXT,
                subdomains TEXT,      -- json list
                entities TEXT,        -- json list
                keywords TEXT,        -- json list
                tags TEXT,            -- json list
                importance REAL,
                created_at TEXT,
                updated_at TEXT,
                embedding_id TEXT
            );
            CREATE TABLE IF NOT EXISTS relationships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                src_id TEXT,
                dst_id TEXT,
                rel_type TEXT,
                weight REAL,
                UNIQUE(src_id, dst_id, rel_type)
            );
            CREATE INDEX IF NOT EXISTS idx_notes_domain ON notes(domain);
            CREATE INDEX IF NOT EXISTS idx_rel_src ON relationships(src_id);
            """
        )
        self.vector.executescript(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                note_id TEXT,
                chunk_index INTEGER,
                text TEXT,
                embedding TEXT,       -- json list[float]
                dim INTEGER,
                model TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_note ON chunks(note_id);
            """
        )
        self.agents.executescript(
            """
            CREATE TABLE IF NOT EXISTS agents (
                name TEXT PRIMARY KEY,
                domain TEXT,
                note_count INTEGER,
                config TEXT            -- json
            );
            """
        )
        for c in (self.metadata, self.vector, self.agents):
            c.commit()

    def reset(self):
        self.metadata.executescript("DELETE FROM notes; DELETE FROM relationships;")
        self.vector.executescript("DELETE FROM chunks;")
        self.agents.executescript("DELETE FROM agents;")
        for c in (self.metadata, self.vector, self.agents):
            c.commit()

    def close(self):
        for c in (self.metadata, self.vector, self.agents):
            c.close()
