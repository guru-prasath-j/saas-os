"""Deterministic dashboard aggregates from vault frontmatter (no LLM, instant)."""
from __future__ import annotations
import re


def _find(notes, path):
    for n in notes:
        if n.path == path:
            return n
    return None


def build(notes) -> dict:
    by_cat = {}
    for n in notes:
        by_cat[n.category or "?"] = by_cat.get(n.category or "?", 0) + 1

    projects = [n for n in notes if n.meta.get("type") == "project" and n.path.startswith("01_Profile/Projects/")]
    lang = {}
    group = {}
    for p in projects:
        L = (p.meta.get("language") or "").strip()
        if L and L.lower() != "n/a":
            lang[L] = lang.get(L, 0) + 1
        g = p.meta.get("subcategory") or "Other"
        group[g] = group.get(g, 0) + 1

    experiences = []
    for n in notes:
        if n.meta.get("type") == "experience":
            experiences.append({"title": n.title, "role": n.meta.get("role",""), "period": n.meta.get("period",""), "location": n.meta.get("location",""), "summary": n.meta.get("summary", "")})
    certs = [n.title for n in notes if n.meta.get("type") == "certification"]
    skills = [re.sub(r"^Skills — ", "", n.title) for n in notes if n.meta.get("type") == "skill"]

    # profile bits from About Me
    am = _find(notes, "01_Profile/About Me.md")
    name, title, location = "Guruprasath J", "Flutter + AI Engineer", ""
    if am:
        b = am.body
        m = re.search(r"\*\*(.+?)\*\*", b)
        if m: name = m.group(1)
        if "Software Development Engineer" in b: title = "Software Development Engineer — Flutter & AI"
        ml = re.search(r"Location:\*\*\s*([^\n]+)", b)
        if ml: location = ml.group(1).strip()

    return {
        "profile": {"name": name, "title": title, "location": location},
        "counts": {
            "notes": len(notes), "projects": len(projects),
            "experiences": len(experiences), "certifications": len(certs),
            "skill_areas": len(skills),
        },
        "projects_by_language": dict(sorted(lang.items(), key=lambda x: -x[1])),
        "projects_by_group": group,
        "experiences": experiences,
        "certifications": certs,
        "skills": skills,
        "by_category": by_cat,
    }
