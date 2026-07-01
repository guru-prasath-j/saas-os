"""Knowledge relationship graph.

Auto-infers edges between notes:
  - `references`  : note A wiki-links [[B]] (explicit link)
  - `related_to`  : notes share entities/keywords (weighted by overlap)
Manual edges (e.g. `depends_on`) can be added via `add()`.

Stored in metadata.db `relationships(src_id, dst_id, rel_type, weight)`.
"""
from __future__ import annotations

import json
import re

from .metadata import note_id, _WIKILINK

_MIN_OVERLAP = 2          # shared keywords/entities needed for related_to


class RelationshipEngine:
    def __init__(self, dbs):
        self.dbs = dbs

    def add(self, src_id: str, dst_id: str, rel_type: str, weight: float = 1.0):
        self.dbs.metadata.execute(
            "INSERT OR REPLACE INTO relationships (src_id,dst_id,rel_type,weight) "
            "VALUES (?,?,?,?)", (src_id, dst_id, rel_type, weight))
        self.dbs.metadata.commit()

    def build(self, notes) -> int:
        title_to_id = {n.title.lower(): note_id(n.path) for n in notes}
        meta = {note_id(n.path): n for n in notes}
        rows = []

        # explicit wiki-link references
        for n in notes:
            src = note_id(n.path)
            for target in _WIKILINK.findall(n.body or ""):
                tid = title_to_id.get(target.strip().lower())
                if tid and tid != src:
                    rows.append((src, tid, "references", 1.0))

        # related_to via shared keyword/entity overlap
        sigs = {}
        for n in notes:
            toks = set(re.findall(r"[a-z0-9]+", (n.title + " " + (n.body or "")).lower()))
            sigs[note_id(n.path)] = toks
        ids = list(sigs)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                overlap = len(sigs[ids[i]] & sigs[ids[j]])
                if overlap >= max(_MIN_OVERLAP, 8):  # require meaningful overlap
                    w = round(min(1.0, overlap / 50), 3)
                    rows.append((ids[i], ids[j], "related_to", w))

        if rows:
            self.dbs.metadata.executemany(
                "INSERT OR REPLACE INTO relationships (src_id,dst_id,rel_type,weight) "
                "VALUES (?,?,?,?)", rows)
            self.dbs.metadata.commit()
        return len(rows)

    def neighbors(self, nid: str) -> list[dict]:
        rs = self.dbs.metadata.execute(
            "SELECT dst_id, rel_type, weight FROM relationships WHERE src_id=? "
            "UNION SELECT src_id, rel_type, weight FROM relationships WHERE dst_id=?",
            (nid, nid)).fetchall()
        return [{"id": r[0], "rel_type": r[1], "weight": r[2]} for r in rs]

    def graph(self) -> list[dict]:
        rs = self.dbs.metadata.execute(
            "SELECT src_id, dst_id, rel_type, weight FROM relationships").fetchall()
        return [{"src": r[0], "dst": r[1], "rel_type": r[2], "weight": r[3]} for r in rs]
