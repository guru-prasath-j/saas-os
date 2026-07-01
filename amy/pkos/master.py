"""Master agent — orchestrates router + registry and merges responses.

For a multi-intent query it invokes each matched domain agent and merges their
answers, with combined (de-duplicated) source attribution.
"""
from __future__ import annotations


class MasterAgent:
    def __init__(self, registry: dict, router, llm=None):
        self.registry = registry
        self.router = router
        self.llm = llm

    def handle(self, query: str, extra_context: str = "") -> dict:
        domains = self.router.route(query)
        sections = []
        sources: list[str] = []
        for d in domains:
            agent = self.registry.get(d)
            if agent is None:
                continue
            res = agent.answer(query, llm=self.llm, extra_context=extra_context)
            if res.get("abstained"):
                continue   # agent had nothing relevant -> stays silent
            sections.append(res)
            for s in res["sources"]:
                if s not in sources:
                    sources.append(s)

        # everyone abstained -> fall back to the general agent over the whole vault
        if not sections and "general" in self.registry:
            res = self.registry["general"].answer(query, llm=self.llm, extra_context=extra_context)
            sections.append(res)
            sources = list(res["sources"])

        if not sections:
            return {"query": query, "domains": [], "answer":
                    "I couldn't find anything relevant in your notes.", "sources": []}

        if len(sections) == 1:
            answer = sections[0]["answer"]
        else:
            answer = "\n\n".join(f"**{s['domain']}**\n{s['answer']}" for s in sections)

        return {
            "query": query,
            "domains": [s["domain"] for s in sections],
            "answer": answer,
            "sections": sections,        # per-domain answers + their sources
            "sources": sources,          # combined, de-duplicated
        }
