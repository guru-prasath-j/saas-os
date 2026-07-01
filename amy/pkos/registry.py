"""Dynamic agent registry.

One runtime DomainAgent per detected domain — no physical Python files generated.
Each agent retrieves within its own domain's notes (lightweight keyword scoring)
and answers via an optional LLM, always returning its source files.
"""
from __future__ import annotations

from ..knowledge.retrieval import hybrid_search, is_relevant
from ..knowledge.embeddings import HashingEmbedder

# one cheap local embedder shared by all in-memory domain searches
_LOCAL_EMBEDDER = HashingEmbedder()


class DomainAgent:
    def __init__(self, name: str, notes: list):
        self.name = name
        self.notes = notes
        self.persona = (
            f"You are the {name} agent in a Personal Knowledge OS. Answer the user's "
            f"question using ONLY their {name} notes provided as context. Be concise, "
            f"cite the notes you used, and if the notes don't contain the answer say so "
            f"plainly instead of guessing."
        )

    def answer(self, query: str, llm=None, k: int = 5, extra_context: str = "") -> dict:
        ranked = hybrid_search(query, self.notes, embedder=_LOCAL_EMBEDDER, k=k)
        # abstention: a specialist agent stays silent when nothing is relevant
        # (the 'general' fallback agent always answers)
        if self.name != "general" and not is_relevant(ranked):
            return {"domain": self.name, "answer": "", "model": "abstained",
                    "abstained": True, "sources": []}

        notes = [r["note"] for r in ranked]
        note_ctx = "\n\n---\n".join(
            f"## {n.title} ({n.path})\n{(n.body or '')[:800]}" for n in notes)
        context = (f"# Conversation so far\n{extra_context}\n\n# Notes\n{note_ctx}"
                   if extra_context else note_ctx)
        if llm is not None:
            text, model = llm.generate(self.persona, query, context)
        elif context:
            text, model = f"From your {self.name} notes:\n{context[:600]}", "none"
        else:
            text, model = f"You have no {self.name} notes yet.", "none"
        return {"domain": self.name, "answer": text, "model": model,
                "sources": [n.path for n in notes]}


def build_registry(notes, domain_map: dict[str, list[str]]) -> dict[str, DomainAgent]:
    by_path = {n.path: n for n in notes}
    registry: dict[str, DomainAgent] = {}
    for domain, paths in domain_map.items():
        registry[domain] = DomainAgent(domain, [by_path[p] for p in paths if p in by_path])
    # whole-vault fallback agent for queries that match no specific domain
    # (prevents fanning a generic question out to every agent)
    registry["general"] = DomainAgent("general", list(notes))
    return registry
