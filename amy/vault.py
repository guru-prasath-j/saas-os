"""Load notes + YAML frontmatter from the Obsidian vault."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from . import config

try:
    import frontmatter as _fm
except Exception:
    _fm = None


@dataclass
class Note:
    path: str            # vault-relative path
    title: str
    meta: dict = field(default_factory=dict)
    body: str = ""

    @property
    def category(self): return self.meta.get("category", "")
    @property
    def owner(self): return self.meta.get("owner", "")
    @property
    def tags(self): return self.meta.get("tags", []) or []

    @property
    def sensitive(self) -> bool:
        if any(m in self.path for m in config.SENSITIVE_PATH_MARKERS):
            return True
        if self.owner in config.SENSITIVE_OWNERS:
            return True
        return any(t in config.SENSITIVE_TAGS for t in self.tags)


def _tiny_parse(text: str):
    """Minimal frontmatter parser used when python-frontmatter is unavailable."""
    meta, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            raw, body = text[3:end], text[end + 4:]
            key = None
            for line in raw.splitlines():
                if re.match(r"^\s*-\s+", line) and key:
                    meta.setdefault(key, [])
                    if isinstance(meta[key], list):
                        meta[key].append(line.strip()[2:].strip().strip('"'))
                elif ":" in line:
                    key, _, val = line.partition(":")
                    key, val = key.strip(), val.strip()
                    if val.startswith("[") and val.endswith("]"):
                        meta[key] = [v.strip() for v in val[1:-1].split(",") if v.strip()]
                    elif val:
                        meta[key] = val.strip('"')
                    else:
                        meta[key] = []
    return meta, body.strip()


def _note_from_file(p: Path, vault: Path) -> Note:
    rel = str(p.relative_to(vault)).replace("\\", "/")
    text = p.read_text(encoding="utf-8", errors="ignore")
    if _fm is not None:
        post = _fm.loads(text)
        meta, body = dict(post.metadata), post.content
    else:
        meta, body = _tiny_parse(text)
    title = meta.get("title") or p.stem
    return Note(path=rel, title=title, meta=meta, body=body)


def load_one(rel_path: str, vault: Path | None = None) -> Note | None:
    """Load a single note by vault-relative path (used for hot-reloading captures)."""
    vault = Path(vault or config.VAULT)
    p = vault / rel_path
    if not p.exists():
        return None
    return _note_from_file(p, vault)


def load_notes(vault: Path | None = None) -> list[Note]:
    vault = Path(vault or config.VAULT)
    notes: list[Note] = []
    for p in vault.rglob("*.md"):
        rel = str(p.relative_to(vault)).replace("\\", "/")
        if rel.startswith("_Amy/") or rel.startswith("_Jarvis/"):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if _fm is not None:
            post = _fm.loads(text)
            meta, body = dict(post.metadata), post.content
        else:
            meta, body = _tiny_parse(text)
        title = meta.get("title") or p.stem
        notes.append(Note(path=rel, title=title, meta=meta, body=body))
    return notes
