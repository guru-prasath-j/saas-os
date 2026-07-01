"""Metadata engine — derive structured metadata for every note and store it in
metadata.db. Never modifies the original markdown.

Fields: id, title, summary, domain, subdomains, entities, keywords, tags,
importance score, created_at, updated_at, embedding_id.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path

from ..pkos import analyzer, domains as domainmod

_W = re.compile(r"[a-z0-9']+")
_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
_ENTITY = re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+){0,3})\b")
_STOP = set("the a an and or but if then of to in on for with at by from is are was "
            "were be been being this that these those it its my your our their his her "
            "i you we they he she as not no do does did have has had will would can could "
            "about into over under more most some any all can't".split())


def note_id(path: str) -> str:
    return hashlib.sha1(path.encode()).hexdigest()[:16]


def _keywords(note, n: int = 10) -> list[str]:
    toks = [t for t in _W.findall((note.title + " " + (note.body or "")).lower())
            if t not in _STOP and len(t) > 2]
    return [w for w, _ in Counter(toks).most_common(n)]


def _entities(note, n: int = 12) -> list[str]:
    body = note.body or ""
    ents = set(_WIKILINK.findall(body))
    for m in _ENTITY.findall(body):
        if m.lower() not in _STOP:
            ents.add(m)
    return sorted(ents)[:n]


def _subdomains(path: str) -> list[str]:
    parts = path.split("/")[:-1]  # folders, excluding filename
    return [re.sub(r"^\d+[_\-\s]*", "", p) for p in parts if p]


def _importance(note) -> float:
    body = note.body or ""
    headings = len(analyzer.extract_headings(body))
    links = len(_WIKILINK.findall(body))
    length = len(body)
    tags = len(note.tags or [])
    raw = headings * 3 + links * 4 + tags * 2 + min(length / 400, 15)
    return round(min(100.0, raw), 1)


def _times(path: str, vault_root, meta: dict) -> tuple[str, str]:
    created = meta.get("created")
    updated = meta.get("updated")
    if (not created or not updated) and vault_root:
        try:
            st = os.stat(Path(vault_root) / path)
            created = created or _dt.datetime.fromtimestamp(st.st_ctime).isoformat()
            updated = updated or _dt.datetime.fromtimestamp(st.st_mtime).isoformat()
        except Exception:
            pass
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    return str(created or now), str(updated or now)


class MetadataEngine:
    def __init__(self, dbs, llm=None):
        self.dbs = dbs
        self.llm = llm

    def build(self, notes, domain_map=None, vault_root=None) -> int:
        dm = domain_map or domainmod.detect(notes)
        # invert domain_map -> per-note primary domain
        primary = {}
        for dom, paths in dm.items():
            for p in paths:
                primary.setdefault(p, dom)

        cur = self.dbs.metadata
        rows = []
        for note in notes:
            nid = note_id(note.path)
            created, updated = _times(note.path, vault_root, note.meta or {})
            rows.append((
                nid, note.path, note.title,
                analyzer.summarize(note, llm=self.llm),
                primary.get(note.path, "general"),
                json.dumps(_subdomains(note.path)),
                json.dumps(_entities(note)),
                json.dumps(_keywords(note)),
                json.dumps(list(note.tags or [])),
                _importance(note),
                created, updated,
                nid,  # embedding_id == note id (chunks are keyed by note_id)
            ))
        cur.executemany(
            "INSERT OR REPLACE INTO notes "
            "(id,path,title,summary,domain,subdomains,entities,keywords,tags,"
            " importance,created_at,updated_at,embedding_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        cur.commit()
        return len(rows)

    def get(self, nid: str) -> dict | None:
        r = self.dbs.metadata.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
        return _row_to_meta(r) if r else None

    def all(self) -> list[dict]:
        rs = self.dbs.metadata.execute("SELECT * FROM notes").fetchall()
        return [_row_to_meta(r) for r in rs]

    def filter(self, domain=None, tags=None) -> list[dict]:
        out = []
        for m in self.all():
            if domain and m["domain"] != domain:
                continue
            if tags and not (set(tags) & set(m["tags"])):
                continue
            out.append(m)
        return out


def _row_to_meta(r) -> dict:
    return {
        "id": r["id"], "path": r["path"], "title": r["title"], "summary": r["summary"],
        "domain": r["domain"], "subdomains": json.loads(r["subdomains"] or "[]"),
        "entities": json.loads(r["entities"] or "[]"),
        "keywords": json.loads(r["keywords"] or "[]"),
        "tags": json.loads(r["tags"] or "[]"),
        "importance": r["importance"], "created_at": r["created_at"],
        "updated_at": r["updated_at"], "embedding_id": r["embedding_id"],
    }
