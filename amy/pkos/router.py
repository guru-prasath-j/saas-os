"""Intent router — single and multi-intent.

Returns every domain a query touches (e.g. "how's my money and my parents" ->
["finance", "family"]). Falls back to all available domains / general when nothing
matches, so the master can still answer.
"""
from __future__ import annotations

from .domains import DEFAULT_KEYWORDS


class IntentRouter:
    def __init__(self, available_domains, keywords: dict | None = None):
        self.available = list(available_domains)
        self.kw = keywords or DEFAULT_KEYWORDS

    def route(self, query: str) -> list[str]:
        q = (query or "").lower()
        hits: list[str] = []
        for dom in self.available:
            if dom == "general":
                continue   # 'general' is the fallback, never a keyword match
            words = self.kw.get(dom, [dom])
            if dom in q or any(w in q for w in words):
                if dom not in hits:
                    hits.append(dom)
        if hits:
            return hits
        # nothing matched -> ONE general agent over the whole vault (never fan out to all)
        if "general" in self.available:
            return ["general"]
        return self.available[:1]
