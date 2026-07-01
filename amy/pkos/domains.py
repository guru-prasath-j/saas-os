"""Domain detection.

Maps each note to one or more domains by **content** (keywords in title/tags/body)
with a **folder-name** fallback. So a note about money lands in `finance` even if
its folder isn't named that — matching the PKOS spec examples.

DEFAULT_KEYWORDS is extensible; pass your own mapping to `detect`.
"""
from __future__ import annotations

import re

DEFAULT_KEYWORDS: dict[str, list[str]] = {
    "finance":  ["money", "finance", "budget", "salary", "invest", "investment", "savings",
                 "expense", "spending", "bank", "payment", "income", "tax", "loan", "debt",
                 "afford", "cost", "price", "spend"],
    "calendar": ["calendar", "schedule", "event", "appointment", "reminder",
                 "due date", "deadline", "upcoming", "renewal", "bill due",
                 "what's coming", "what is coming", "when is", "next month"],
    "family":   ["family", "parents", "parent", "mom", "mother", "dad", "father", "wife",
                 "husband", "kids", "children", "son", "daughter", "home"],
    "career":   ["work", "job", "career", "office", "project", "meeting", "client",
                 "promotion", "resume", "interview", "manager", "deadline"],
    "health":   ["gym", "health", "fitness", "workout", "run", "running", "diet", "sleep",
                 "doctor", "medical", "weight", "calories", "exercise"],
    "learning": ["learn", "learning", "study", "course", "book", "reading", "tutorial",
                 "skill", "certification", "lecture", "research"],
}


def _folder_domain(path: str) -> str:
    top = path.split("/", 1)[0]
    s = re.sub(r"^\d+[_\-\s]*", "", top)
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or "general"


def _haystack(note) -> str:
    parts = [note.title or "", " ".join(note.tags or []), (note.body or "")[:2000]]
    return " ".join(parts).lower()


def detect(notes, keywords: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """Return {domain: [note_paths]}.

    A note is assigned to every content domain it matches; if it matches none, it
    falls back to its folder-derived domain.
    """
    kw = keywords or DEFAULT_KEYWORDS
    mapping: dict[str, list[str]] = {}
    for n in notes:
        text = _haystack(n)
        domains = {d for d, words in kw.items() if any(w in text for w in words)}
        if not domains:
            domains = {_folder_domain(n.path)}
        for d in domains:
            mapping.setdefault(d, []).append(n.path)
    return mapping
