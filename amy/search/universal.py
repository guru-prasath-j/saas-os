"""Universal Search Engine.

    User -> UniversalSearch -> source adapters -> aggregator -> ranked response

Sources: vault (semantic+keyword hybrid), gmail/calendar/tasks (connectors),
memories (conversation summaries), goals. Features: hybrid search, confidence
scoring, source attribution, filters (sources=), pagination (limit/offset).
"""
from __future__ import annotations

ALL_SOURCES = ["vault", "email", "calendar", "tasks", "memories", "goals"]


class UniversalSearch:
    def __init__(self, notes, collab_db, connector_dir=None):
        self.notes = notes
        self.collab_db = collab_db
        self.connector_dir = connector_dir

    # --- per-source adapters (each returns scored, source-tagged hits) ------
    def _vault(self, query, terms):
        from ..knowledge.retrieval import hybrid_search
        from ..knowledge.embeddings import HashingEmbedder
        out = []
        for r in hybrid_search(query, self.notes, HashingEmbedder(), 20):
            if r["score"] <= 0:
                continue
            out.append({"source": "vault", "title": r["note"].title,
                        "ref": r["note"].path, "score": round(float(r["score"]), 4)})
        return out

    def _memories(self, terms):
        rows = self.collab_db.conn.execute(
            "SELECT text FROM summaries ORDER BY id DESC LIMIT 100").fetchall()
        return [{"source": "memories", "title": r["text"][:80], "ref": "", "score": 0.4}
                for r in rows if any(w in r["text"].lower() for w in terms)]

    def _goals(self, terms):
        rows = self.collab_db.conn.execute("SELECT id,title,domain FROM goals").fetchall()
        return [{"source": "goals", "title": r["title"], "ref": r["id"], "score": 0.55}
                for r in rows if any(w in r["title"].lower() for w in terms)]

    def _connector(self, kind, terms):
        if not self.connector_dir:
            return []
        try:
            from ..connectors import ConnectorRegistry
            items = ConnectorRegistry(self.connector_dir).list(kind, mode="private", limit=100)
        except Exception:
            return []
        out = []
        for it in items:
            hay = (it.get("title", "") + " " + it.get("body", "")).lower()
            if any(w in hay for w in terms):
                out.append({"source": kind, "title": it.get("title", ""),
                            "ref": it.get("id", ""), "score": 0.45})
        return out

    # --- aggregate + rank + paginate ---------------------------------------
    def search(self, query: str, sources=None, limit: int = 10, offset: int = 0) -> dict:
        terms = [w for w in query.lower().split() if len(w) > 1]
        want = set(sources) if sources else None

        def use(s):
            return want is None or s in want

        results = []
        if use("vault"):
            results += self._vault(query, terms)
        if use("memories"):
            results += self._memories(terms)
        if use("goals"):
            results += self._goals(terms)
        for kind in ("email", "calendar", "tasks"):
            if use(kind):
                results += self._connector(kind, terms)

        results.sort(key=lambda x: x["score"], reverse=True)
        total = len(results)
        page = results[offset:offset + limit]
        confidence = round(min(100.0, page[0]["score"] * 100), 1) if page else 0.0
        return {
            "query": query, "total": total, "limit": limit, "offset": offset,
            "confidence": confidence,
            "sources_searched": [s for s in ALL_SOURCES if use(s)],
            "results": page,
        }
