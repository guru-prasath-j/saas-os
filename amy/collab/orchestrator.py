"""CollabMaster — multi-agent collaboration over the vault.

    User -> Master -> Memory Manager -> Intent Router -> Multiple Agents -> Planner -> Merge

Ties the PKOS multi-intent agents together with the Planner participant and the
Memory Manager / Agent Cards. Reflection and Learning agents read the same store.
"""
from __future__ import annotations

from ..pkos import build_pkos
from .db import CollabDB
from .memory import MemoryManager
from .cards import AgentCards
from .planner import PlannerAgent
from .reflection import ReflectionAgent
from .learning import LearningAgent


class CollabMaster:
    def __init__(self, notes, db_path, llm=None, vault_path=None,
                 finance_db_path=None, connector_dir=None):
        self.db = CollabDB(db_path)
        self.llm = llm
        self.vault_path = vault_path   # used for live filesystem memory recall
        self._finance_db_path = finance_db_path
        self._connector_dir = connector_dir
        self.pkos_master, self.registry, self.domain_map = build_pkos(notes, llm=llm)
        # Inject CalendarAgent (data-driven, not vault-note-driven)
        try:
            from ..agents.calendar import CalendarAgent
            cal = CalendarAgent(
                notes=notes,
                finance_db_path=finance_db_path,
                connector_dir=connector_dir,
            )
            self.pkos_master.registry["calendar"] = cal
            if "calendar" not in self.pkos_master.router.available:
                self.pkos_master.router.available.append("calendar")
        except Exception:
            pass
        self.memory = MemoryManager(self.db)
        self.cards = AgentCards(self.db)
        self.cards.build(self.registry, self.domain_map)
        from ..events import EventStore, register_default_triggers
        self.events = EventStore(self.db)
        self.planner = PlannerAgent(self.db, llm, events=self.events)
        self.reflection = ReflectionAgent(self.db, self.planner, self.memory, llm)
        self.learning = LearningAgent(self.db, self.memory)
        register_default_triggers(self.events, self.memory)
        from ..product.marketplace import Marketplace
        self.marketplace = Marketplace(self.db)

    def handle(self, query: str) -> dict:
        self.memory.log_activity("query", query)
        # tier 1: conversation turns + prefs (always)
        conv = self.memory.conversation_context()
        # tier 2: episodic recall — reads 00_Daily + 09_Memory live from disk
        # so notes written earlier this session are always searchable
        try:
            from ..memory.recall import MemoryRecall
            all_notes = (self.pkos_master.registry.get("general") or object()).notes
            recalled = MemoryRecall(
                all_notes, collab_db=self.db, vault_path=self.vault_path
            ).context_block(query)
        except Exception:
            recalled = ""
        finance_ctx = self._finance_context(query)
        extra_context = "\n\n".join(
            p for p in (conv, recalled, finance_ctx) if p)
        res = self.pkos_master.handle(query, extra_context=extra_context)   # multi-agent + memory context
        disabled = self.marketplace.disabled_set()
        # marketplace: drop disabled domain agents
        sections = [s for s in res["sections"] if f"{s['domain']}_agent" not in disabled]
        domains = [s["domain"] for s in sections]
        sources = []
        for s in sections:
            for src in s["sources"]:
                if src not in sources:
                    sources.append(src)

        for d in domains:
            self.memory.log_activity("agent", query, domain=d)
            self.cards.record_question(f"{d}_agent", query)
        for s in sources:
            self.memory.record_access(s)

        # Planner joins the collaboration when the query implies planning
        if PlannerAgent.wants_plan(query) and "planner_agent" not in disabled:
            ctx = "\n".join(sec["answer"] for sec in sections)[:2000]
            sections.append(self.planner.plan(query, context=ctx))
            domains.append("planner")

        if not sections:
            answer = "All matching agents are disabled in the marketplace."
        elif len(sections) == 1:
            answer = sections[0]["answer"]
        else:
            answer = "\n\n".join(f"**{s['domain']}**\n{s['answer']}" for s in sections)

        self.memory.add_turn(query, answer)   # store the turn so the next reply has context
        self.events.emit("query.asked", {"query": query, "domains": domains}, source="chat")
        return {"query": query, "domains": domains, "answer": answer,
                "sections": sections, "sources": sources}

    def _finance_context(self, query: str) -> str:
        """Inject structured finance data for finance-domain queries."""
        if not self._finance_db_path:
            return ""
        from ..pkos.domains import DEFAULT_KEYWORDS
        kw = DEFAULT_KEYWORDS.get("finance", [])
        q = query.lower()
        if not ("finance" in q or any(w in q for w in kw)):
            return ""
        import os
        if not os.path.exists(self._finance_db_path):
            return ""
        try:
            from ..finance import FinanceEngine
            fe = FinanceEngine(self._finance_db_path)
            try:
                return fe.context_block()
            finally:
                fe.close()
        except Exception:
            return ""

    def close(self):
        self.db.close()
