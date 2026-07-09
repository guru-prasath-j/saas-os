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
                 finance_db_path=None, connector_dir=None, mcp_connectors=None):
        self.db = CollabDB(db_path)
        self.llm = llm
        self.vault_path = vault_path   # used for live filesystem memory recall
        self._finance_db_path = finance_db_path
        self._connector_dir = connector_dir
        # [{name, server_url, auth_type, auth_value, auth_extra}, ...] for this
        # user's registered MCP sources (amy/saas/db.py McpConnector, already
        # decrypted by the caller) — used by _career_context() for live
        # Plane/job-search data. Pre-fetched by the router rather than looked
        # up here, so this package doesn't need to import amy.saas.db.
        self._mcp_connectors = mcp_connectors or []
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
        # Intentionally bare: CollabMaster is the pre-R2 chat/agent layer and
        # uses register_default_triggers (amy/events/triggers.py), not the
        # amy/agents/reactive.py reactive agents that amy.events.factory
        # wires — no event type emitted through self.events here is in
        # AGENT_RELEVANT_EVENTS today.
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
        career_ctx = self._career_context(query)
        captures_ctx = self._captures_context(query)
        extra_context = "\n\n".join(
            p for p in (conv, recalled, finance_ctx, career_ctx, captures_ctx) if p)
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

    def _career_context(self, query: str) -> str:
        """Inject live Plane project data for career-domain queries — same
        shape as _finance_context(). Best-effort only: any failure (no
        connector registered, bad credentials, network issue) silently
        yields no context rather than breaking the chat response, same
        defensive pattern as everywhere else context gets merged in above.

        Only Plane is wired to a real tool call — a job-search MCP connector
        (e.g. jobspy-mcp-server) would match the same keyword gate, but no
        specific tool name is called for it yet since no real server's tool
        list has been verified (see mcp connectors session notes: don't call
        unverified tool names blind)."""
        if not self._mcp_connectors:
            return ""
        from ..pkos.domains import DEFAULT_KEYWORDS
        kw = DEFAULT_KEYWORDS.get("career", [])
        q = query.lower()
        if not ("plane" in q or any(w in q for w in kw)):
            return ""
        plane = next((c for c in self._mcp_connectors if "plane" in c.get("name", "").lower()), None)
        if not plane:
            return ""
        try:
            from ..connectors.mcp import MCPConnector, call_tool_sync
            connector = MCPConnector(
                plane["server_url"], auth_type=plane.get("auth_type", "none"),
                auth_value=plane.get("auth_value"), auth_extra=plane.get("auth_extra"),
            )
            # list_projects is the one Plane tool verified to take no required
            # parameters — safe to call blind. There's no per-user "default
            # project" available server-side to scope a more specific query
            # (that's stored in the browser's localStorage, not the backend).
            # Shorter timeout than call_tool_sync's own default (15s) — this is
            # optional context enrichment for a chat reply, not worth letting
            # an unresponsive Plane server add much to every career-domain
            # question's response time.
            result = call_tool_sync(connector, "list_projects", {}, timeout=8.0)
            if not result or result.get("is_error"):
                return ""
            text = (result.get("text") or "")[:2000]
            return f"Live Plane data ({plane['name']}):\n{text}" if text else ""
        except Exception:
            return ""

    def _captures_context(self, query: str) -> str:
        """Inject photo-memory facts for queries that match ingested captures
        (place / OCR / caption / tags / user note). Relevance-gated inside
        context_block() itself — no keyword gate here, so 'that poster in
        Bangalore' matches without the word 'photo' appearing. Best-effort:
        any failure silently yields no context, same defensive pattern as
        _finance_context/_career_context above."""
        if not self.vault_path:
            return ""
        try:
            from ..captures import context_block
            return context_block(query, vault=self.vault_path)
        except Exception:
            return ""

    def close(self):
        self.db.close()
