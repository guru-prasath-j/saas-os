from __future__ import annotations
from dataclasses import dataclass, field
from .. import config, tools
from ..retrieval import Retriever
from ..llm import LLMRouter
from ..vault import Note


@dataclass
class AgentResult:
    agent: str
    answer: str
    sources: list[str] = field(default_factory=list)
    sensitive: bool = False
    model: str = ""


class SubAgent:
    """A domain expert scoped to part of the vault.

    can_write   -> whether this agent may propose note writes (per-agent tool).
    write_kinds -> human-readable list of what it is allowed to record.
    """
    name = "base"
    persona = "You are a helpful assistant. Answer ONLY from the provided context."
    can_write = False
    write_kinds: list[str] = []

    def __init__(self, retriever: Retriever, llm: LLMRouter):
        self.retriever = retriever
        self.llm = llm
        self.scopes = config.AGENT_SCOPES.get(self.name, [])

    def retrieve(self, query: str, k: int = 5) -> list[Note]:
        return self.retriever.search(query, scope_prefixes=self.scopes, k=k)

    BROAD_CUES = ("list", "all ", "every", "each", "entire", "complete", "overview of", "rundown")

    def answer(self, query: str, retrieval_query: str | None = None,
               extra_context: str | None = None) -> AgentResult:
        rq = retrieval_query or query
        broad = any(c in query.lower() for c in self.BROAD_CUES)
        k = 40 if broad else 6
        notes = self.retrieve(rq, k=k)
        sensitive = any(n.sensitive for n in notes)
        budget = 600 if broad else 1200          # more notes -> smaller slice each
        context = "\n\n---\n".join(f"## {n.title} ({n.path})\n{n.body[:budget]}" for n in notes)
        # Phase 4: prepend recalled memory so the reply depends on the memory lake
        if extra_context:
            context = f"{extra_context}\n\n---\n{context}"
        text, model = self.llm.generate(self.persona, query, context, sensitive=sensitive)
        return AgentResult(self.name, text, [n.path for n in notes], sensitive, model)

    def propose_write(self, query: str):
        """Per-agent write tool. Returns a WriteProposal scoped to THIS agent,
        or None if the agent isn't allowed to write."""
        if not self.can_write:
            return None
        notes = self.retrieve(query)
        # only allow targets inside this agent's own scope
        scoped = [n for n in notes if not self.scopes or any(n.path.startswith(p) for p in self.scopes)]
        return tools.propose(self.name, query, scoped or notes)
