"""GitHub → vault project-notes sync (additions only).

Closes the loop the one-time Projects import left open: new GitHub repos
never appeared as notes under 01_Profile/Projects/, so everything reading
the vault (dashboard projects card, legacy Portfolio tab, CollabMaster
profile context) stayed frozen at import time while Career Autopilot's
live GitHub reads moved on.

Hard rules:
  - ADDITIONS ONLY. A repo already covered by any existing note (matched
    by its repo: frontmatter URL, or a filename equal to the repo name) is
    skipped — the sync never edits, overwrites, or deletes a note, so
    manual curation is always safe.
  - Real data only: everything in the generated note comes from the GitHub
    API response (no LLM involved anywhere).
  - Same frontmatter shape as the existing notes (type: project etc.), so
    dashboard/aggregation filters pick new notes up unchanged.

Driven by the github_projects_sync job (daily). No-ops cleanly when no
GitHub connector is registered.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

_log = logging.getLogger("amy.projects_sync")

_PROJECTS_SUBDIR = Path("01_Profile") / "Projects"
_REPO_LINE_RE = re.compile(r"^repo:\s*(\S+)\s*$", re.MULTILINE)


def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "unnamed"


def _existing_coverage(projects_dir: Path) -> tuple[set[str], set[str]]:
    """(repo URLs referenced by any note's frontmatter, note filename stems)
    — both lowercased, URLs without a trailing slash."""
    urls: set[str] = set()
    stems: set[str] = set()
    if not projects_dir.exists():
        return urls, stems
    for p in projects_dir.glob("*.md"):
        stems.add(p.stem.lower())
        try:
            m = _REPO_LINE_RE.search(p.read_text(encoding="utf-8", errors="ignore")[:2000])
            if m:
                urls.add(m.group(1).strip().strip('"').rstrip("/").lower())
        except Exception:
            continue
    return urls, stems


def _note_body(repo: dict) -> str:
    name = str(repo.get("name") or "unnamed")
    url = str(repo.get("html_url") or repo.get("url") or "")
    owner = str((repo.get("owner") or {}).get("login") or "") if isinstance(
        repo.get("owner"), dict) else ""
    language = str(repo.get("language") or "")
    visibility = "private" if repo.get("private") else "public"
    description = str(repo.get("description") or "").strip()
    lines = [
        "---",
        f"id: project-{_safe_filename(name).lower()}",
        f'title: "{name}"',
        "category: Projects",
        "subcategory: New / Unsorted",
        "type: project",
    ]
    if owner:
        lines.append(f"owner: {owner}")
    if language:
        lines.append(f"language: {language}")
    lines += [
        f"visibility: {visibility}",
        f"repo: {url}",
        "related:",
        '  - "[[01_Profile/Skills]]"',
        '  - "[[01_Profile/Projects]]"',
        "status: active",
        "priority: medium",
        "---",
        "",
        description or "(no description on GitHub yet)",
        "",
        "> Auto-created by github_projects_sync from the GitHub connector — "
        "edit freely; the sync only ever ADDS notes, it never overwrites one.",
        "",
    ]
    return "\n".join(lines)


def github_projects_sync(ctx) -> dict:
    """Job handler: list repos via the GitHub connector, write a vault note
    for each repo no existing note covers. Additions only (module docstring)."""
    from . import tools
    from .saas import tenancy

    try:
        out = tools.invoke(ctx, "portfolio_repo_list", {}, actor="agent")
        repos = out.get("repos") or []
    except Exception as exc:
        return {"skipped": f"github repo list unavailable: {str(exc)[:160]}"}
    if not repos:
        return {"skipped": "no repositories returned"}

    vault = tenancy.resolve_vault_dir(ctx.user_id)
    projects_dir = vault / _PROJECTS_SUBDIR
    projects_dir.mkdir(parents=True, exist_ok=True)
    known_urls, known_stems = _existing_coverage(projects_dir)

    created: list[str] = []
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        name = str(repo.get("name") or "").strip()
        url = str(repo.get("html_url") or repo.get("url") or "").rstrip("/")
        if not name or repo.get("fork"):
            continue   # forks aren't the user's own project work
        if url.lower() in known_urls or _safe_filename(name).lower() in known_stems:
            continue
        target = projects_dir / f"{_safe_filename(name)}.md"
        if target.exists():
            continue   # filename race safety — never overwrite
        try:
            target.write_text(_note_body(repo), encoding="utf-8")
            created.append(name)
            known_stems.add(_safe_filename(name).lower())
            if url:
                known_urls.add(url.lower())
        except Exception as exc:
            _log.warning("projects_sync: could not write note for %r: %s", name, exc)

    if created:
        try:
            ns = ctx.notify_store()
            ref = "projects_sync"
            if not ns.exists_today("projects_sync", ref):
                ns.create(
                    type="projects_sync",
                    title=f"{len(created)} new project note(s) added to the vault",
                    body="From GitHub: " + ", ".join(created[:8])
                         + ("…" if len(created) > 8 else "")
                         + " — under 01_Profile/Projects (additions only, "
                           "nothing overwritten).",
                    priority="normal",
                    related_entity={"entity_type": "vault", "id": ref})
        except Exception:
            pass

    return {"repos_seen": len(repos), "notes_created": len(created),
           "created": created}
