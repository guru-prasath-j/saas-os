"""Deterministic counting/aggregation over vault frontmatter.

RAG can't count (it only sees top-k chunks). For "how many / count of X" queries
we answer exactly from the loaded notes' metadata — no LLM guessing."""
from __future__ import annotations

COUNT_TRIGGERS = ("how many", "how much", "count", "number of", "exact count",
                  "total number", "total count")

# (keywords, label, predicate over a Note). Order matters: more specific first.
SUBJECTS = [
    (["work experience", "experience", "jobs", "roles", "employers", "companies i", "worked"],
     "work experiences", lambda n: n.meta.get("type") == "experience"),
    (["certification", "certs", "certificate"],
     "certifications", lambda n: n.meta.get("type") == "certification"),
    (["project", "projects", "apps", "repos", "repositories"],
     "projects", lambda n: n.meta.get("type") == "project" and n.path.startswith("01_Profile/Projects/")),
    (["skill area", "skill", "skills"],
     "skill areas", lambda n: n.meta.get("type") == "skill"),
    (["business", "businesses"],
     "businesses", lambda n: n.meta.get("type") == "business"),
    (["member", "payout recipient", "holder", "holders"],
     "SBI holders", lambda n: "SBI Account/Holders/" in n.path),
    (["note", "notes", "total"],
     "notes", lambda n: True),
]


def is_count_query(text: str) -> bool:
    return any(t in text.lower() for t in COUNT_TRIGGERS)


def answer_count(text: str, notes):
    low = text.lower()
    for kws, label, pred in SUBJECTS:
        if any(k in low for k in kws):
            items = [n for n in notes if pred(n)]
            names = ", ".join(sorted({n.title for n in items}))
            return label, len(items), names, [n.path for n in items]
    return None
