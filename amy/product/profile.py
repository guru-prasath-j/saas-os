"""Profile Builder — automatically synthesize a user profile from the vault:
skills, projects, interests, goals.

Heuristic + offline by default; reuses PKOS domain detection and the note
analyzer. Goals come from the planner if a CollabDB is provided.
"""
from __future__ import annotations

import re
from collections import Counter

from ..pkos import domains as domainmod
from ..pkos import analyzer

_W = re.compile(r"[a-z0-9+#.]+")
_STOP = set("the a an and or but if then of to in on for with at by from is are was "
            "were be this that it my your our notes note about how what i you we they "
            "will can could should would do does did have has had".split())

# domains whose notes are treated as "projects"
_PROJECT_DOMAINS = {"projects", "career", "work"}
# domains that signal skills/learning
_SKILL_DOMAINS = {"learning", "career", "projects", "skills"}


def _top_keywords(notes, n=15):
    toks = []
    for note in notes:
        toks += [t for t in _W.findall((note.title + " " + (note.body or "")).lower())
                 if t not in _STOP and len(t) > 2]
    return [w for w, _ in Counter(toks).most_common(n)]


class ProfileBuilder:
    def __init__(self, notes, collab_db=None):
        self.notes = notes
        self.collab_db = collab_db
        self.domain_map = domainmod.detect(notes)

    def _notes_in(self, domains: set):
        paths = set()
        for d in domains:
            paths.update(self.domain_map.get(d, []))
        return [n for n in self.notes if n.path in paths]

    def skills(self) -> list[str]:
        return _top_keywords(self._notes_in(_SKILL_DOMAINS) or self.notes, n=15)

    def projects(self) -> list[dict]:
        out = []
        for n in self._notes_in(_PROJECT_DOMAINS):
            out.append({"title": n.title, "path": n.path,
                        "summary": analyzer.summarize(n, max_len=160)})
        return out

    def interests(self) -> list[str]:
        # interests = the domains the user actually has, ranked by note count
        return [d for d, _ in sorted(self.domain_map.items(),
                                     key=lambda kv: -len(kv[1]))]

    def goals(self) -> list[dict]:
        if not self.collab_db:
            return []
        rs = self.collab_db.conn.execute(
            "SELECT title, domain, status, progress FROM goals ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rs]

    def build(self) -> dict:
        return {
            "skills": self.skills(),
            "projects": self.projects(),
            "interests": self.interests(),
            "goals": self.goals(),
            "note_count": len(self.notes),
        }


def build_profile(notes, collab_db=None) -> dict:
    return ProfileBuilder(notes, collab_db).build()
