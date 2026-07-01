"""Unified Memory (PIOS v2).

One `recall(query)` across all five sources — Vault, Gmail, Calendar, Tasks,
Conversations — returning a merged, source-tagged result. Reuses existing pieces
(hybrid retrieval, ConnectorRegistry, MemoryManager); adds no new storage.
"""
from __future__ import annotations


class UnifiedMemory:
    def __init__(self, notes, collab_db, connector_dir=None):
        self.notes = notes
        self.connector_dir = connector_dir
        from ..collab.memory import MemoryManager
        self.memory = MemoryManager(collab_db)

    def _match(self, text, terms):
        t = (text or "").lower()
        return any(w in t for w in terms)

    def recall(self, query: str, k: int = 5) -> dict:
        from ..knowledge.retrieval import hybrid_search
        from ..knowledge.embeddings import HashingEmbedder
        terms = [w for w in query.lower().split() if len(w) > 2]

        vault = [{"source": "vault", "title": r["note"].title, "path": r["note"].path,
                  "score": r["score"]}
                 for r in hybrid_search(query, self.notes, HashingEmbedder(), k)]

        conversations = [{"source": "conversation", "text": s["text"]}
                         for s in self.memory.recent_summaries(40)
                         if self._match(s["text"], terms)][:k]

        conn = {"email": [], "calendar": [], "tasks": []}
        if self.connector_dir:
            try:
                from ..connectors import ConnectorRegistry
                reg = ConnectorRegistry(self.connector_dir)
                for kind in ("email", "calendar", "tasks"):
                    items = reg.list(kind, mode="private", limit=50)
                    conn[kind] = [{"source": kind, "title": it.get("title", ""), "ts": it.get("ts", "")}
                                  for it in items
                                  if self._match(it.get("title", "") + " " + it.get("body", ""), terms)][:k]
            except Exception:
                pass

        merged = vault + conversations + conn["email"] + conn["calendar"] + conn["tasks"]
        return {
            "query": query,
            "vault": vault,
            "conversations": conversations,
            "email": conn["email"],
            "calendar": conn["calendar"],
            "tasks": conn["tasks"],
            "count": len(merged),
        }
