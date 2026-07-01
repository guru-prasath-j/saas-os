"""Entity index + auto-linking (Phase 3).

Turns free-text journal entries into graph-connected notes by detecting mentions
of *known entities* — your projects, goals, skills, and people — and emitting
``[[wikilinks]]`` to them plus ``#tags``. Deterministic and offline: it matches
entity names that actually appear in the text, so links point at real notes that
exist in your vault (which keeps Obsidian's graph clean, no dangling links).

Sources of entities:
  * vault note titles (projects, knowledge, people, …)
  * goal titles + domains from collab.db
  * a small set of category tags
"""
from __future__ import annotations

import re

# folders whose notes are high-value link targets (others still match, lower pri)
_PRIORITY_PREFIXES = ("project", "people", "person", "knowledge", "area", "moc")
_STOP = {"the", "and", "for", "with", "note", "notes", "daily", "memory",
         "untitled", "readme", "index", "home", "inbox"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


class EntityIndex:
    def __init__(self):
        # normalized name -> canonical display name (what we [[link]] to)
        self._names: dict[str, str] = {}
        self._tags: set[str] = set()

    # --- building -------------------------------------------------------
    def add(self, name: str):
        n = _norm(name)
        if len(n) < 3 or n in _STOP:
            return
        # skip pure numbers / dates
        if re.fullmatch(r"[\d\-/.]+", n):
            return
        self._names.setdefault(n, name.strip())

    def add_tag(self, tag: str):
        t = re.sub(r"[^\w]", "", (tag or "").lower())
        if t and t not in _STOP:
            self._tags.add(t)

    @classmethod
    def from_sources(cls, notes=None, collab_db=None) -> "EntityIndex":
        idx = cls()
        for n in (notes or []):
            title = getattr(n, "title", None)
            if title:
                idx.add(title)
            cat = getattr(n, "category", None)
            if cat:
                idx.add_tag(cat)
        if collab_db is not None:
            try:
                for r in collab_db.conn.execute("SELECT title, domain FROM goals").fetchall():
                    if r["title"]:
                        idx.add(r["title"])
                    if r["domain"]:
                        idx.add_tag(r["domain"])
            except Exception:
                pass
        return idx

    # --- matching -------------------------------------------------------
    def extract(self, text: str, max_links: int = 5) -> tuple[list[str], list[str]]:
        """Return (links, tags) found in `text`. Links are canonical names whose
        mention appears in the text; tags are known category tags mentioned."""
        if not text:
            return [], []
        low = text.lower()
        links: list[str] = []
        # match longer names first so "machine learning" wins over "learning"
        for norm in sorted(self._names, key=len, reverse=True):
            if len(links) >= max_links:
                break
            canonical = self._names[norm]
            if canonical in links:
                continue
            if " " in norm:
                if norm in low:                      # phrase containment
                    links.append(canonical)
            else:
                if re.search(rf"\b{re.escape(norm)}\b", low):  # word boundary
                    links.append(canonical)
        tags = [t for t in self._tags if re.search(rf"\b{re.escape(t)}\b", low)]
        return links, tags[:5]

    def __len__(self) -> int:
        return len(self._names)
