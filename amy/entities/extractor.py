"""Entity Extractor — pull people, orgs, topics, and links from vault notes.

Uses heuristics only (no ML/API required):
  * [[wikilinks]]  → references between notes
  * @mentions      → person names
  * Capitalized phrases (2-3 words) → person or org
  * #tags          → topics
"""
from __future__ import annotations

import datetime as _dt
import re
import sqlite3
import uuid
from collections import Counter

_STOP = frozenset({
    "the","a","an","and","or","but","in","on","at","to","for","of","with","by",
    "from","is","was","are","were","be","been","have","has","had","do","did",
    "does","will","would","should","could","may","might","that","this","these",
    "those","it","its","as","so","if","than","then","not","no","all","each",
    "every","more","most","some","any","only","also","just","very","much",
    "how","what","when","where","who","why","which","amy","note","notes",
    "today","week","month","year","day","new","old","first","last","next",
    "monday","tuesday","wednesday","thursday","friday","saturday","sunday",
})

_ORG_SUFFIXES = frozenset({
    "inc","ltd","corp","co","llc","technologies","university","institute",
    "foundation","group","labs","systems","solutions","services","media",
})


def _extract(text: str) -> dict[str, list[str]]:
    out: dict[str, list] = {"person": [], "org": [], "topic": [], "wikilink": []}
    out["wikilink"] = re.findall(r"\[\[([^\]|#]+?)(?:\|[^\]]*)?\]\]", text)
    out["person"] += [m.strip() for m in re.findall(r"@(\w[\w ]{1,28})", text)]
    for phrase in re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b", text):
        words = phrase.split()
        if any(w.lower() in _STOP for w in words):
            continue
        if words[-1].lower() in _ORG_SUFFIXES:
            out["org"].append(phrase)
        else:
            out["person"].append(phrase)
    out["topic"] = [t.lower() for t in re.findall(r"#(\w{2,30})", text)]
    return out


class EntityExtractor:
    def __init__(self, db_path):
        self.db = sqlite3.connect(str(db_path))
        self.db.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY, name TEXT, type TEXT,
                mentions INTEGER DEFAULT 1, note_paths TEXT DEFAULT '',
                last_seen TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ent_type ON entities(type);
            CREATE INDEX IF NOT EXISTS idx_ent_name ON entities(name);
        """)
        self.db.commit()

    def build(self, notes) -> dict:
        counts: Counter = Counter()
        paths_map: dict[tuple, list] = {}
        for note in notes:
            text = (note.title or "") + " " + (note.body or "")
            for etype, names in _extract(text).items():
                for raw in names:
                    name = raw.strip().lower()
                    if not name or len(name) < 2 or name in _STOP:
                        continue
                    key = (name, etype)
                    counts[key] += 1
                    if note.path not in paths_map.get(key, []):
                        paths_map.setdefault(key, []).append(note.path)

        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        for (name, etype), count in counts.items():
            eid = uuid.uuid5(uuid.NAMESPACE_URL, f"{etype}:{name}").hex
            note_paths = ",".join(paths_map.get((name, etype), [])[:10])
            self.db.execute(
                "INSERT INTO entities (id,name,type,mentions,note_paths,last_seen) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "mentions=excluded.mentions, note_paths=excluded.note_paths, "
                "last_seen=excluded.last_seen",
                (eid, name, etype, count, note_paths, now),
            )
        self.db.commit()
        return {"extracted": len(counts)}

    def list_entities(self, type: str | None = None,
                      limit: int = 100, min_mentions: int = 2) -> list[dict]:
        if type:
            rows = self.db.execute(
                "SELECT * FROM entities WHERE type=? AND mentions>=? "
                "ORDER BY mentions DESC LIMIT ?",
                (type, min_mentions, limit),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM entities WHERE mentions>=? "
                "ORDER BY mentions DESC LIMIT ?",
                (min_mentions, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def search(self, q: str, limit: int = 30) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM entities WHERE name LIKE ? "
            "ORDER BY mentions DESC LIMIT ?",
            (f"%{q.lower()}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.db.close()
