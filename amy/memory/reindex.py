"""VaultReindex (Phase 6) — make Obsidian the source of truth.

Under vault-as-truth, the markdown in the vault is canonical and SQLite is a
disposable index. This module proves and enforces that: it scans the journaled
folders (00_Daily, 09_Memory), takes inventory of what's there, reconciles it
against the collab `events` table (drift detection), and can **rebuild**
structured rows (decisions) from the markdown — so if collab.db is lost or wiped,
the important state can be reconstructed from the vault alone.

Note retrieval is already vault-as-truth: the engine loads + embeds every .md in
the vault, so daily/memory notes are searchable without any reindex. This module
covers the *structured* state (events/decisions) that otherwise lived only in
SQLite.

Idempotent: rebuilds key off the in-note ``eid`` markers, so re-running never
duplicates.
"""
from __future__ import annotations

import re
from pathlib import Path

from .writer import DAILY_DIR, MEMORY_DIR

_EID = re.compile(r"<!--\s*eid:([A-Za-z0-9]+)\s*-->")
_FM_TYPE = re.compile(r"^type:\s*(\w+)", re.M)
_FM_DATE = re.compile(r"^date:\s*(\S+)", re.M)
_FM_CREATED = re.compile(r"^created:\s*(\S+)", re.M)
_TITLE = re.compile(r"^#\s+(.+)$", re.M)
_CAT = re.compile(r"^- Category:\s*(\w+)", re.M)
_CONF = re.compile(r"^- Confidence:\s*([0-9.]+|None)", re.M)
_REASON = re.compile(r"^- Reason:\s*(.*)$", re.M)


class VaultReindex:
    def __init__(self, vault_path):
        self.vault = Path(vault_path)

    # --- inventory ------------------------------------------------------
    def scan(self) -> dict:
        """Inventory the journaled vault: eids seen + atomic memory notes."""
        daily_eids: set[str] = set()
        for p in sorted((self.vault / DAILY_DIR).glob("*.md")) if (self.vault / DAILY_DIR).exists() else []:
            daily_eids.update(_EID.findall(p.read_text(encoding="utf-8", errors="ignore")))
        memory_notes: list[dict] = []
        mem_dir = self.vault / MEMORY_DIR
        if mem_dir.exists():
            for p in sorted(mem_dir.glob("*.md")):
                txt = p.read_text(encoding="utf-8", errors="ignore")
                eids = _EID.findall(txt)
                memory_notes.append({
                    "path": str(p.relative_to(self.vault)),
                    "type": (_FM_TYPE.search(txt) or [None, None])[1] if _FM_TYPE.search(txt) else None,
                    "title": (_TITLE.search(txt).group(1).strip() if _TITLE.search(txt) else p.stem),
                    "eid": eids[0] if eids else None,
                })
        return {
            "daily_eid_count": len(daily_eids),
            "daily_eids": sorted(daily_eids),
            "memory_note_count": len(memory_notes),
            "memory_notes": memory_notes,
        }

    # --- reconcile against SQLite --------------------------------------
    def verify(self, collab_db) -> dict:
        """Compare vault eids against the events table → drift report."""
        inv = self.scan()
        vault_eids = set(inv["daily_eids"]) | {n["eid"] for n in inv["memory_notes"] if n["eid"]}
        rows = collab_db.conn.execute("SELECT id FROM events").fetchall()
        db_eids = {r["id"] for r in rows}
        return {
            "in_vault": len(vault_eids),
            "in_db": len(db_eids),
            "missing_from_vault": sorted(db_eids - vault_eids),   # journaling gap
            "missing_from_db": sorted(vault_eids - db_eids),      # DB lost/wiped
            "in_sync": vault_eids == db_eids or not (db_eids - vault_eids),
        }

    # --- rebuild structured rows from markdown -------------------------
    def rebuild_decisions(self, collab_db) -> dict:
        """Recreate decision rows from 09_Memory decision notes. Idempotent:
        a deterministic id derived from the note's eid prevents duplicates."""
        mem_dir = self.vault / MEMORY_DIR
        if not mem_dir.exists():
            return {"rebuilt": 0, "skipped": 0}
        rebuilt = skipped = 0
        for p in sorted(mem_dir.glob("*.md")):
            txt = p.read_text(encoding="utf-8", errors="ignore")
            if not (_FM_TYPE.search(txt) and _FM_TYPE.search(txt).group(1) == "decision"):
                continue
            eid = (_EID.findall(txt) or [None])[0]
            if not eid:
                continue
            did = f"v_{eid}"  # stable id from the vault marker
            exists = collab_db.conn.execute(
                "SELECT 1 FROM decisions WHERE id=?", (did,)).fetchone()
            if exists:
                skipped += 1
                continue
            title = _TITLE.search(txt).group(1).strip() if _TITLE.search(txt) else p.stem
            cat = _CAT.search(txt).group(1) if _CAT.search(txt) else "personal"
            conf_raw = _CONF.search(txt).group(1) if _CONF.search(txt) else "None"
            conf = None if conf_raw == "None" else float(conf_raw)
            reason = _REASON.search(txt).group(1).strip() if _REASON.search(txt) else ""
            ts = _FM_CREATED.search(txt).group(1) if _FM_CREATED.search(txt) else \
                (_FM_DATE.search(txt).group(1) if _FM_DATE.search(txt) else "")
            collab_db.conn.execute(
                "INSERT INTO decisions (id, ts, title, reason, domain, confidence, outcome, status) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (did, ts, title, reason, cat, conf, None, "open"))
            rebuilt += 1
        collab_db.conn.commit()
        return {"rebuilt": rebuilt, "skipped": skipped}
