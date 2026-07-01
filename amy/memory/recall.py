"""MemoryRecall (Phase 4) — make chat depend on the memory lake.

Retrieves the *relevant slice* of journaled memory for a query and formats it as
a context block the chat can lean on. This is what turns "memory is stored" into
"the assistant remembers": before answering, the master recalls the handful of
past daily-note / atomic-memory entries that matter for this question and feeds
them to the responding agent.

Key properties
--------------
* **Memory-scoped** — searches only the journaled folders (00_Daily, 09_Memory)
  plus recent conversation summaries, so it surfaces *remembered* context, not
  ordinary domain notes (the agent already retrieves those).
* **Relevance-gated** — returns nothing when nothing is relevant, so irrelevant
  memories never pollute a reply (more context is not always better).
* **Offline** — uses the hashing embedder by default, matching the chat-time
  embedding policy (no network, no cost).
"""
from __future__ import annotations

from pathlib import Path as _Path

from .writer import DAILY_DIR, MEMORY_DIR

_MEM_PREFIXES = (DAILY_DIR + "/", MEMORY_DIR + "/")
_MEM_FOLDERS = (DAILY_DIR, MEMORY_DIR)


def _load_vault_memory(vault_path) -> list:
    """Read 00_Daily + 09_Memory directly from the filesystem — always fresh,
    never stale from an engine cache. Returns a list of Note-compatible objects."""
    from ..vault import Note, _tiny_parse
    vault = _Path(vault_path)
    notes = []
    for folder in _MEM_FOLDERS:
        d = vault / folder
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md"), reverse=True):
            text = f.read_text(encoding="utf-8", errors="ignore")
            meta, body = _tiny_parse(text)
            rel = f"{folder}/{f.name}"
            notes.append(Note(path=rel, title=f.stem, meta=meta, body=body))
    return notes


class MemoryRecall:
    def __init__(self, notes, collab_db=None, embedder=None, min_score: float = 0.15,
                 vault_path=None):
        self._cached_notes = notes or []
        self._vault_path = vault_path   # when set, always reads fresh from disk
        self.collab_db = collab_db
        self.min_score = min_score
        self._embedder = embedder

    def _emb(self):
        if self._embedder is None:
            from ..knowledge.embeddings import HashingEmbedder
            self._embedder = HashingEmbedder()
        return self._embedder

    def _memory_notes(self):
        # prefer live filesystem read so notes written this session are searchable
        if self._vault_path:
            return _load_vault_memory(self._vault_path)
        return [n for n in self._cached_notes
                if any((n.path or "").startswith(p) for p in _MEM_PREFIXES)]

    def recall(self, query: str, k: int = 3) -> list[dict]:
        """Return up to k relevant memory entries above the relevance gate."""
        mem = self._memory_notes()
        out: list[dict] = []
        if mem:
            from ..knowledge.retrieval import hybrid_search
            for r in hybrid_search(query, mem, self._emb(), k):
                if r["score"] >= self.min_score:
                    n = r["note"]
                    out.append({"source": "memory", "title": n.title, "path": n.path,
                                "snippet": (n.body or "")[:300], "score": round(r["score"], 3)})
        # conversation summaries (keyword fallback — cheap, optional)
        if self.collab_db is not None:
            terms = [w for w in query.lower().split() if len(w) > 2]
            try:
                from ..collab.memory import MemoryManager
                for s in MemoryManager(self.collab_db).recent_summaries(40):
                    t = (s["text"] or "").lower()
                    if terms and any(w in t for w in terms):
                        out.append({"source": "conversation", "title": "past conversation",
                                    "path": None, "snippet": s["text"][:300], "score": None})
                        if len([x for x in out if x["source"] == "conversation"]) >= k:
                            break
            except Exception:
                pass
        return out

    def context_block(self, query: str, k: int = 3) -> str:
        """Formatted block to prepend to an agent's context, or '' if nothing
        relevant was recalled."""
        hits = self.recall(query, k=k)
        if not hits:
            return ""
        lines = ["## Relevant memory (from your vault)"]
        for h in hits:
            tag = h["title"] if h["source"] == "memory" else "conversation"
            lines.append(f"- ({tag}) {h['snippet'].strip()}")
        return "\n".join(lines)
