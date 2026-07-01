"""KnowledgeBase — facade tying the knowledge layer together.

    Markdown -> Metadata -> Embeddings -> Relationship graph -> Agents -> LLM

build(notes): populate metadata.db, vector.db, agent_registry.db + relationships.
ask(query):  semantic search -> context -> (optional LLM answer) + confidence + sources.
"""
from __future__ import annotations

import json

from ..pkos import domains as domainmod
from .db import KnowledgeDBs
from .embeddings import HashingEmbedder, EmbeddingEngine, make_embedder
from .metadata import MetadataEngine, note_id
from .relationships import RelationshipEngine
from .search import SemanticSearch


class KnowledgeBase:
    def __init__(self, data_dir, embedder=None, llm=None):
        self.dbs = KnowledgeDBs(data_dir)
        # default embedder via factory: NVIDIA (free) -> OpenAI -> sentence-transformers -> hashing
        self.embedder = embedder or make_embedder()
        self.llm = llm
        # summaries use the free heuristic by default (no per-note LLM cost on build);
        # the LLM is used for answering in ask(). Pass summary_llm=llm to opt in.
        self.metadata = MetadataEngine(self.dbs, llm=None)
        self.embeddings = EmbeddingEngine(self.dbs, self.embedder)
        self.relationships = RelationshipEngine(self.dbs)
        self.search_engine = SemanticSearch(self.dbs, self.embeddings, self.metadata)

    def build(self, notes, vault_root=None, rebuild=True) -> dict:
        if rebuild:
            self.dbs.reset()
        domain_map = domainmod.detect(notes)
        n_meta = self.metadata.build(notes, domain_map, vault_root=vault_root)
        n_chunks = 0
        for note in notes:
            n_chunks += self.embeddings.build_for_note(note_id(note.path),
                                                        f"{note.title}\n\n{note.body or ''}")
        n_rel = self.relationships.build(notes)
        self._register_agents(domain_map)
        return {"notes": n_meta, "chunks": n_chunks, "relationships": n_rel,
                "domains": len(domain_map), "embedder": self.embedder.name}

    def _register_agents(self, domain_map):
        cur = self.dbs.agents
        cur.execute("DELETE FROM agents")
        cur.executemany(
            "INSERT OR REPLACE INTO agents (name, domain, note_count, config) VALUES (?,?,?,?)",
            [(f"{d}_agent", d, len(p), json.dumps({"domain": d})) for d, p in domain_map.items()])
        cur.commit()

    def ask(self, query, domain=None, tags=None, k=5) -> dict:
        res = self.search_engine.search(query, domain=domain, tags=tags, k=k)
        if self.llm is not None and res["context"]:
            answer, model = self.llm.generate(
                "Answer using ONLY the provided context. Cite the source files.",
                query, res["context"])
        elif res["context"]:
            answer, model = "Based on your knowledge base:\n" + res["context"][:600], "none"
        else:
            answer, model = "I couldn't find anything relevant in your knowledge base.", "none"
        return {
            "query": query, "answer": answer, "model": model,
            "sources": res["sources"], "confidence": res["confidence"],
            "chunks": res["chunks"],
        }

    def agents(self) -> list[dict]:
        rs = self.dbs.agents.execute("SELECT name, domain, note_count FROM agents").fetchall()
        return [{"name": r[0], "domain": r[1], "note_count": r[2]} for r in rs]

    def close(self):
        self.dbs.close()
