"""PKOS — Personal Knowledge Operating System.

A clean, self-contained layer that turns a loaded Obsidian vault into domain
agents and answers multi-intent queries with source attribution. Each component
(analyzer, domains, registry, router, master) is independently testable and has
no FastAPI/DB dependencies — it operates on plain Note objects + an optional LLM.

Pipeline:  query -> MasterAgent -> IntentRouter -> AgentRegistry -> DomainAgent -> vault notes
"""
from .analyzer import analyze, analyze_vault, extract_headings, summarize
from .domains import detect, DEFAULT_KEYWORDS
from .registry import build_registry, DomainAgent
from .router import IntentRouter
from .master import MasterAgent


def build_pkos(notes, llm=None, keywords=None):
    """Construct the full PKOS stack from a list of vault notes.
    Returns (master, registry, domain_map)."""
    domain_map = detect(notes, keywords)
    registry = build_registry(notes, domain_map)
    router = IntentRouter(list(registry.keys()), keywords)
    master = MasterAgent(registry, router, llm)
    return master, registry, domain_map
