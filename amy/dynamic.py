"""Dynamic, per-vault agents (SaaS mode).

Instead of the hardcoded personal layout (00_Home, 02_Family, ...), this builds
one agent per top-level folder found in whatever vault the user uploaded. A
"Work / Health / Recipes" vault automatically gets Work, Health and Recipes
agents. A general agent (whole-vault scope) is always added as a safe fallback.

Enabled with AMY_DYNAMIC_AGENTS=1. The personal vault keeps its tailored agents
when the flag is off.
"""
from __future__ import annotations

import re
from .agents.base import SubAgent
from .retrieval import Retriever
from .llm import LLMRouter

# folders that are never their own domain
_SKIP = {"attachments", ".obsidian", ".git", "_amy", "_jarvis", "templates"}


def _slug(folder: str) -> str:
    """'01_Profile' -> 'profile', 'Job Search' -> 'job-search'."""
    s = re.sub(r"^\d+[_\-\s]*", "", folder)          # strip leading "01_"
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or folder.lower()


def _keywords(folder: str) -> list[str]:
    s = re.sub(r"^\d+[_\-\s]*", "", folder)
    return [w for w in re.split(r"[^a-zA-Z0-9]+", s.lower()) if len(w) > 1]


def discover_domains(notes) -> list[dict]:
    """Return [{name, folder, keywords}] for each top-level folder with notes."""
    seen: dict[str, dict] = {}
    for n in notes:
        top = n.path.split("/", 1)[0]
        if not top or top.lower() in _SKIP:
            continue
        name = _slug(top)
        if name not in seen:
            seen[name] = {"name": name, "folder": top, "keywords": _keywords(top), "count": 0}
        seen[name]["count"] += 1
    # stable order: most populated folders first
    return sorted(seen.values(), key=lambda d: -d["count"])


class GenericAgent(SubAgent):
    """A folder-scoped agent created at runtime for an arbitrary vault folder."""
    can_write = False

    def __init__(self, name: str, folder: str, retriever: Retriever, llm: LLMRouter):
        self.name = name
        pretty = folder.replace("_", " ").strip()
        self.persona = (
            f"You are the '{pretty}' agent. You answer questions about the user's "
            f"notes in their '{pretty}' folder. Answer ONLY from the provided context; "
            f"if the notes don't cover it, say you don't have that yet."
        )
        super().__init__(retriever, llm)
        self.scopes = [folder]          # override the config-based scope


class GeneralAgent(SubAgent):
    """Whole-vault fallback agent (searches everything)."""
    name = "general"
    can_write = False
    persona = ("You are the user's general assistant over their whole vault. "
               "Answer ONLY from the provided context; if it isn't there, say so.")

    def __init__(self, retriever: Retriever, llm: LLMRouter):
        super().__init__(retriever, llm)
        self.scopes = []                # empty scope = search all notes


def build_agents(retriever: Retriever, llm: LLMRouter, notes) -> tuple[dict, list[dict]]:
    domains = discover_domains(notes)
    agents: dict[str, SubAgent] = {d["name"]: GenericAgent(d["name"], d["folder"], retriever, llm)
                                   for d in domains}
    agents["general"] = GeneralAgent(retriever, llm)
    return agents, domains


class DynamicClassifier:
    """Routes a query to one of the discovered folder-domains.

    Keyword-first (match query words against folder names), then an LLM fallback
    constrained to the discovered domains, then the 'general' whole-vault agent.
    """
    def __init__(self, llm: LLMRouter, domains: list[dict]):
        self.llm = llm
        self.domains = domains

    def _keyword(self, query: str):
        q = query.lower()
        best, score = None, 0
        for d in self.domains:
            s = sum(1 for kw in d["keywords"] if kw in q)
            if s > score:
                best, score = d["name"], s
        return best, score

    def classify(self, query: str) -> tuple[str, str]:
        name, score = self._keyword(query)
        if score >= 1:
            return name, f"keyword:{score}"
        # LLM fallback, constrained to discovered domains
        if self.domains:
            names = [d["name"] for d in self.domains]
            sys = ("Pick the ONE folder that best fits the request from this list: "
                   + ", ".join(names) + ". If none clearly fit, reply 'general'. "
                   'Reply ONLY JSON: {"domain":"<one>"}.')
            try:
                out, model = self.llm.generate(sys, query, "", sensitive=False)
                if model != "template":
                    import json
                    data = json.loads(out[out.find("{"): out.rfind("}") + 1])
                    got = str(data.get("domain", "")).lower()
                    if got in names:
                        return got, f"llm:{model}"
            except Exception:
                pass
        return "general", "default"
