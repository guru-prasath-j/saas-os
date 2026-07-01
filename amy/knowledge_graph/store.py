"""Graph store (graph.db) — cross-source nodes + typed edges, with traversal.

Node types: note, email, calendar, task, goal, memory.
Relationship types: depends_on, related_to, supports, blocks, belongs_to.
Plain sqlite3; per-user file. Separate from the note-only relationship graph in
the knowledge layer (that one is left untouched).
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

NODE_TYPES = ["note", "email", "calendar", "task", "goal", "memory"]
REL_TYPES = ["depends_on", "related_to", "supports", "blocks", "belongs_to"]


class GraphStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, type TEXT, label TEXT, ref TEXT);
            CREATE TABLE IF NOT EXISTS edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT, src TEXT, dst TEXT, rel TEXT, weight REAL,
                created_at TEXT, updated_at TEXT,
                UNIQUE(src, dst, rel));
            CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
            CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
            CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
            """
        )
        # Migrate existing DBs that lack the timestamp columns
        for col in ("created_at", "updated_at"):
            try:
                self.conn.execute(f"ALTER TABLE edges ADD COLUMN {col} TEXT")
            except Exception:
                pass  # column already exists
        self.conn.commit()

    def reset(self):
        self.conn.executescript("DELETE FROM nodes; DELETE FROM edges;")
        self.conn.commit()

    def add_node(self, node_id, type, label, ref=""):
        self.conn.execute("INSERT OR REPLACE INTO nodes (id,type,label,ref) VALUES (?,?,?,?)",
                          (node_id, type, label, ref))

    def add_edge(self, src, dst, rel, weight=1.0):
        if rel not in REL_TYPES:
            raise ValueError(f"unknown rel '{rel}'")
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO edges (src,dst,rel,weight,created_at,updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(src,dst,rel) DO UPDATE SET weight=excluded.weight, updated_at=excluded.updated_at",
            (src, dst, rel, weight, now, now)
        )

    def commit(self):
        self.conn.commit()

    # --- query --------------------------------------------------------------
    def get_node(self, node_id):
        r = self.conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return dict(r) if r else None

    def nodes(self, type=None, limit=1000):
        if type:
            rs = self.conn.execute("SELECT * FROM nodes WHERE type=? LIMIT ?", (type, limit)).fetchall()
        else:
            rs = self.conn.execute("SELECT * FROM nodes LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rs]

    def edges(self, limit=5000):
        return [dict(r) for r in self.conn.execute(
            "SELECT src,dst,rel,weight,created_at,updated_at FROM edges LIMIT ?",
            (limit,)).fetchall()]

    def neighbors(self, node_id, rel=None):
        q = ("SELECT dst AS other, rel, weight FROM edges WHERE src=? "
             "UNION SELECT src AS other, rel, weight FROM edges WHERE dst=?")
        rs = self.conn.execute(q, (node_id, node_id)).fetchall()
        out = [{"id": r["other"], "rel": r["rel"], "weight": r["weight"]} for r in rs]
        if rel:
            out = [o for o in out if o["rel"] == rel]
        return out

    def traverse(self, node_id, depth=2):
        """BFS up to `depth` hops; returns visited node ids with their distance."""
        seen = {node_id: 0}
        frontier = [node_id]
        for d in range(1, depth + 1):
            nxt = []
            for n in frontier:
                for nb in self.neighbors(n):
                    if nb["id"] not in seen:
                        seen[nb["id"]] = d
                        nxt.append(nb["id"])
            frontier = nxt
            if not frontier:
                break
        return [{"id": nid, "distance": dist, "node": self.get_node(nid)}
                for nid, dist in seen.items() if nid != node_id]

    def stats(self):
        n = self.conn.execute("SELECT type, COUNT(*) c FROM nodes GROUP BY type").fetchall()
        e = self.conn.execute("SELECT rel, COUNT(*) c FROM edges GROUP BY rel").fetchall()
        return {"nodes": {r["type"]: r["c"] for r in n}, "edges": {r["rel"]: r["c"] for r in e}}

    def close(self):
        self.conn.close()
