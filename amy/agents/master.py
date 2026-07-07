"""Master orchestrator: guardrails -> count -> route (with memory) -> read | propose write."""
from __future__ import annotations
from dataclasses import dataclass, field
from .. import guardrails, aggregate, config, security
from .. import agent_writeback as tools
from ..retrieval import Retriever
from ..llm import LLMRouter
from ..classifier import IntentClassifier
from .folders import (HomeAgent, ProfileAgent, ProjectsAgent, FamilyAgent, FinancesAgent,
                      CareerAgent, ResourcesAgent, JobSearchAgent, KnowledgeAgent, CapturesAgent)


@dataclass
class MasterResult:
    intent: str
    answer: str
    sources: list = field(default_factory=list)
    sensitive: bool = False
    model: str = ""
    route: str = ""
    refusal: str | None = None
    voice_safe: str = ""
    needs_confirmation: bool = False
    proposal: dict | None = None


class MasterAgent:
    FOLLOWUP_CUES = ("more", "also", "instead", "that way", "those", "them", "it",
                     "including", "and the", "what about", "continue", "again",
                     "attractive", "better", "expand", "elaborate", "same")
    STRONG_FOLLOWUP = ("that way", "this way", "same way", "more attractive", "like that",
                       "in that way", "make it", "reframe", "rephrase", "as well",
                       "more catchy", "better way", "same projects", "those projects")

    def __init__(self, retriever: Retriever, llm: LLMRouter, notes=None):
        self.notes = notes or []
        self.last_intent = None
        self.last_query = None
        if config.DYNAMIC_AGENTS:
            # SaaS mode: build agents from the user's own top-level folders.
            from .. import dynamic
            self.agents, domains = dynamic.build_agents(retriever, llm, self.notes)
            self.classifier = dynamic.DynamicClassifier(llm, domains)
        else:
            # Personal mode: the tailored, hardcoded agents.
            self.classifier = IntentClassifier(llm)
            self.agents = {
                "home":      HomeAgent(retriever, llm),
                "profile":   ProfileAgent(retriever, llm),
                "projects":  ProjectsAgent(retriever, llm),
                "family":    FamilyAgent(retriever, llm),
                "finances":  FinancesAgent(retriever, llm),
                "career":    CareerAgent(retriever, llm),
                "resources": ResourcesAgent(retriever, llm),
                "jobsearch": JobSearchAgent(retriever, llm),
                "knowledge": KnowledgeAgent(retriever, llm),
                "captures":  CapturesAgent(retriever, llm),
            }
        self._pending: dict[str, tools.WriteProposal] = {}
        self._recall = None  # lazy MemoryRecall (Phase 4)

    def _memory_context(self, query: str) -> str | None:
        """Recall relevant journaled memory (00_Daily / 09_Memory) for this query.
        Returns a context block, or None if nothing relevant / unavailable.
        Never raises — memory recall must not break chat."""
        try:
            if self._recall is None:
                from ..memory.recall import MemoryRecall
                self._recall = MemoryRecall(self.notes)
            return self._memory_or_none(self._recall.context_block(query))
        except Exception:
            return None

    @staticmethod
    def _memory_or_none(block: str):
        return block or None

    def _is_followup(self, query: str) -> bool:
        q = query.lower().strip()
        return len(q.split()) <= 8 or any(c in q for c in self.FOLLOWUP_CUES)

    def _is_strong_followup(self, query: str) -> bool:
        q = query.lower()
        return any(c in q for c in self.STRONG_FOLLOWUP)

    def handle(self, query: str, channel: str = "text") -> MasterResult:
        # 1) hard guardrail: never move money / irreversible commands
        verb = guardrails.blocked_action(query)
        if verb:
            msg = (f"I can't {verb.strip()} on your behalf — that's an action you do yourself. "
                   "I can show you who and how much, though.")
            return MasterResult("guardrail", msg, voice_safe=msg, refusal=verb, route="guardrail")

        # 1a) PUBLIC mode: hard-block sensitive intents / writes server-side
        if config.PUBLIC:
            low = query.lower()
            sens_kw = ("pay", "payout", "sbi", "eswari", "sumathi", "vjpn", "ledger", "balance",
                       "budget", "salary i", "farm", "mjvr", "kmd", "daddy", "sathish", "family",
                       "investment", "savings", "bank", "account number")
            wants_write = tools.is_write_request(query)
            if wants_write or any(k in low for k in sens_kw):
                return MasterResult("blocked", security.BLOCKED_MSG, route="public-blocked",
                                    voice_safe="That isn't available in the public demo.")

        # 1b) deterministic COUNT queries -> answer from metadata, not the LLM
        if aggregate.is_count_query(query):
            res = aggregate.answer_count(query, self.notes)
            if res:
                label, n, names, paths = res
                msg = f"You have {n} {label}." + (f"\n\n{names}" if names else "")
                self.last_intent = self.last_intent  # unchanged
                return MasterResult("count", msg, sources=paths[:10], model="aggregate",
                                    route="aggregate", voice_safe=f"You have {n} {label}.")

        # 2) route (with short-term memory for follow-ups)
        intent, route = self.classifier.classify(query)
        kw_score = int(route.split(":")[1]) if route.startswith("keyword:") else 0
        use_memory = False
        if self.last_intent:
            if self._is_strong_followup(query) and kw_score < 2:
                use_memory = True
            elif route == "default" or route.startswith("llm"):
                if self._is_followup(query):
                    use_memory = True
        if use_memory:
            intent, route = self.last_intent, f"memory:{self.last_intent}"
        # public mode: block restricted agents server-side
        if not security.agent_allowed(intent):
            return MasterResult("blocked", security.BLOCKED_MSG, route="public-blocked",
                                voice_safe="That isn't available in the public demo.")
        agent = self.agents[intent]
        retrieval_query = f"{self.last_query} {query}" if (route.startswith("memory") and self.last_query) else query

        # 3) write request? -> delegate to the routed agent's own write tool
        if tools.is_write_request(query):
            prop = agent.propose_write(query)
            if prop is None:
                msg = f"The {intent} agent is read-only — I can't record changes there."
                self.last_intent, self.last_query = intent, query
                return MasterResult(intent, msg, route=route, voice_safe=msg)
            self._pending[prop.id] = prop
            msg = ("This will record a note (no money moves). Confirm to apply:\n\n"
                   + prop.preview + f"\n\nReply confirm:{prop.id} to apply.")
            self.last_intent, self.last_query = intent, query
            return MasterResult(intent, msg, sources=[prop.target], sensitive=prop.sensitive,
                                model="proposal", route=route,
                                voice_safe="I've prepared a change. Confirm on screen to apply.",
                                needs_confirmation=True,
                                proposal={"id": prop.id, "target": prop.target, "preview": prop.preview})

        # 4) normal read (Phase 4: recall relevant memory so replies depend on it)
        mem_ctx = self._memory_context(query)
        res = agent.answer(query, retrieval_query=retrieval_query, extra_context=mem_ctx)
        self.last_intent, self.last_query = intent, query
        voice = guardrails.redact_for_voice(res.answer) if (channel == "voice" and res.sensitive) else res.answer
        return MasterResult(intent, res.answer, res.sources, res.sensitive, res.model,
                            route=route, voice_safe=voice)

    def confirm(self, proposal_id: str) -> MasterResult:
        prop = self._pending.pop(proposal_id, None)
        if not prop:
            return MasterResult("confirm", "No pending change with that id (already applied or expired).",
                                route="confirm")
        status = tools.apply(prop)
        return MasterResult("confirm", f"Done - {status}", sources=[prop.target], sensitive=prop.sensitive,
                            model="write", route="confirm", voice_safe="Change applied.")
