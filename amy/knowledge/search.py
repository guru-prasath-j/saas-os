"""Semantic search engine.

Flow:  query -> metadata filter -> embedding similarity -> retrieve chunks -> context
Returns the assembled context, the chunks, sources, similarities, and a confidence.
"""
from __future__ import annotations

import json

from .embeddings import cosine
from . import confidence


class SemanticSearch:
    def __init__(self, dbs, embed_engine, metadata_engine):
        self.dbs = dbs
        self.embed = embed_engine
        self.meta = metadata_engine

    def search(self, query: str, domain=None, tags=None, k: int = 5) -> dict:
        # 1) metadata filter -> candidate note ids
        candidates = self.meta.filter(domain=domain, tags=tags)
        cand_ids = {m["id"] for m in candidates}

        # 2) load chunks (restricted to candidates if a filter was applied)
        if domain or tags:
            if not cand_ids:
                return _empty(query)
            qmarks = ",".join("?" * len(cand_ids))
            rows = self.dbs.vector.execute(
                f"SELECT id, note_id, text, embedding FROM chunks WHERE note_id IN ({qmarks})",
                tuple(cand_ids)).fetchall()
        else:
            rows = self.dbs.vector.execute(
                "SELECT id, note_id, text, embedding FROM chunks").fetchall()

        if not rows:
            return _empty(query)

        # 3) embedding similarity
        qv = self.embed.query_embedding(query)
        scored = []
        for r in rows:
            sim = cosine(qv, json.loads(r["embedding"]))
            scored.append((sim, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:k]

        # 4) retrieve chunks + sources
        id_to_path = {m["id"]: m["path"] for m in self.meta.all()}
        chunks, sources, sims = [], [], []
        for sim, r in top:
            path = id_to_path.get(r["note_id"], r["note_id"])
            chunks.append({"note_id": r["note_id"], "path": path,
                           "text": r["text"], "similarity": round(sim, 4)})
            sims.append(sim)
            if path not in sources:
                sources.append(path)

        context = "\n\n---\n".join(f"[{c['path']}]\n{c['text']}" for c in chunks)
        return {
            "query": query,
            "context": context,
            "chunks": chunks,
            "sources": sources,
            "confidence": confidence.score(sims, len(sources)),
        }


def _empty(query: str) -> dict:
    return {"query": query, "context": "", "chunks": [], "sources": [], "confidence": 0.0}
