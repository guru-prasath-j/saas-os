"""Public Portfolio mode — a safe, shareable view.

Folder-aware (not keyword-greedy): real projects/skills/roadmap come from the
vault's actual folders (e.g. 01_Profile/Projects, 01_Profile/Skills,
04_Career/Certifications), falling back to keyword domains only when those folders
don't exist. Hard-blocks finance, family, email, memory, and sensitive notes.
"""
from __future__ import annotations

from .profile import ProfileBuilder
from ..pkos import analyzer

PUBLIC_DOMAINS = {"projects", "career", "skills", "learning", "blog", "blogs", "portfolio"}
BLOCKED_DOMAINS = {"finance", "family", "finances", "health", "captures"}

# job-search / application admin folders that should NOT appear as "projects"
_EXCLUDE_FOLDERS = {"job_search", "job search", "interview prep", "resume", "resumes",
                    "cover letters", "networking", "applications", "companies"}


def _folders(note) -> list[str]:
    return [p.lower() for p in note.path.split("/")[:-1]]


def _in_folder(note, name: str) -> bool:
    return name.lower() in _folders(note)


def build_portfolio(notes) -> dict:
    safe = [n for n in notes if not n.sensitive]

    # projects = notes inside a "Projects" folder, minus job-search admin folders
    projects = [n for n in safe if _in_folder(n, "projects")
                and not any(f in _EXCLUDE_FOLDERS for f in _folders(n))]
    # skills = notes inside a "Skills" folder (one per skill area)
    skill_notes = [n for n in safe if _in_folder(n, "skills")]
    # roadmap = certifications + any "learning roadmap" note
    roadmap_notes = [n for n in safe if _in_folder(n, "certifications")
                     or "learning roadmap" in n.title.lower()
                     or _in_folder(n, "learning roadmap")]

    pb = ProfileBuilder(safe)
    # fallbacks (vaults without these folders): keyword domains
    if not projects:
        projects = pb._notes_in({"projects"})
    skills = [n.title for n in skill_notes] or pb.skills()
    roadmap = [n.title for n in roadmap_notes] or [n.title for n in pb._notes_in({"learning"})]

    # drop folder "index" notes (title == folder name) from the project list
    projects = [n for n in projects if n.title.lower() not in ("projects", "skills")]

    return {
        "mode": "public_portfolio",
        "skills": skills,
        "projects": [{"title": n.title, "path": n.path,
                      "summary": analyzer.summarize(n, max_len=160)} for n in projects],
        "interests": [d for d in pb.interests() if d in PUBLIC_DOMAINS],
        "roadmap": sorted(set(roadmap)),
        "blocked": sorted(BLOCKED_DOMAINS),
    }
