"""Spaced Repetition System — SM-2 algorithm over vault notes.

Cards are extracted from notes using two strategies:
  1. Explicit Q:/A: pairs written in the note body
  2. Section headings → first paragraph as context
  3. Title → first sentence fallback
"""
from __future__ import annotations

import datetime as _dt
import re
import sqlite3
import uuid


def _today() -> str:
    return _dt.date.today().isoformat()


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _extract_cards(note_path: str, body: str, title: str) -> list[dict]:
    cards: list[dict] = []
    lines = (body or "").split("\n")

    # Strategy 1: explicit Q:/A: pairs
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        front = None
        if re.match(r"^\*{0,2}Q:\*{0,2}\s+", ln):
            front = re.sub(r"^\*{0,2}Q:\*{0,2}\s*", "", ln).strip()
        if front and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if re.match(r"^\*{0,2}A:\*{0,2}\s+", nxt):
                back = re.sub(r"^\*{0,2}A:\*{0,2}\s*", "", nxt).strip()
                if back:
                    cards.append({"front": front, "back": back})
                i += 2
                continue
        i += 1

    # Strategy 2: headings → first real paragraph below them
    if not cards:
        sections = re.split(r"\n#{1,3} +", "\n" + body)
        for sec in sections[1:4]:
            sec_lines = [l.strip() for l in sec.split("\n") if l.strip() and not l.startswith("#")]
            if len(sec_lines) >= 2:
                heading = sec_lines[0]
                content = " ".join(sec_lines[1:3])[:250]
                if heading and content:
                    cards.append({"front": heading, "back": content})

    # Strategy 3: title → first non-empty paragraph
    if not cards and title:
        paras = [l.strip() for l in (body or "").split("\n")
                 if l.strip() and not l.strip().startswith("#")]
        if paras:
            cards.append({"front": f"What is {title}?", "back": paras[0][:250]})

    return cards


class SRSEngine:
    def __init__(self, db_path):
        self.db = sqlite3.connect(str(db_path))
        self.db.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS srs_cards (
                id TEXT PRIMARY KEY, note_path TEXT, front TEXT, back TEXT,
                interval INTEGER DEFAULT 1, ease REAL DEFAULT 2.5,
                due_date TEXT, reviews INTEGER DEFAULT 0, created_at TEXT
            );
        """)
        self.db.commit()

    # --- build ----------------------------------------------------------
    def build_from_notes(self, notes) -> dict:
        added = 0
        for note in notes:
            for c in _extract_cards(note.path, note.body or "", note.title or ""):
                cid = uuid.uuid5(
                    uuid.NAMESPACE_URL, f"{note.path}:{c['front']}"
                ).hex
                if not self.db.execute(
                    "SELECT 1 FROM srs_cards WHERE id=?", (cid,)
                ).fetchone():
                    self.db.execute(
                        "INSERT INTO srs_cards "
                        "(id,note_path,front,back,due_date,created_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (cid, note.path, c["front"], c["back"], _today(), _now()),
                    )
                    added += 1
        self.db.commit()
        return {"added": added, "total": self._count()}

    # --- review ---------------------------------------------------------
    def due_cards(self, limit: int = 20) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM srs_cards WHERE due_date<=? ORDER BY due_date LIMIT ?",
            (_today(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def review(self, card_id: str, quality: int) -> dict:
        """SM-2: quality 0-5 (0-2=again, 3=hard, 4=good, 5=easy)."""
        row = self.db.execute(
            "SELECT * FROM srs_cards WHERE id=?", (card_id,)
        ).fetchone()
        if not row:
            return {"error": "card not found"}
        ease = max(1.3, row["ease"] + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
        reviews = row["reviews"] + 1
        if quality < 3:
            interval = 1
        elif reviews <= 1:
            interval = 1
        elif reviews == 2:
            interval = 6
        else:
            interval = max(1, round(row["interval"] * ease))
        due = (_dt.date.today() + _dt.timedelta(days=interval)).isoformat()
        self.db.execute(
            "UPDATE srs_cards SET interval=?,ease=?,due_date=?,reviews=? WHERE id=?",
            (interval, round(ease, 3), due, reviews, card_id),
        )
        self.db.commit()
        return {"card_id": card_id, "next_due": due, "interval": interval,
                "ease": round(ease, 2)}

    # --- stats ----------------------------------------------------------
    def stats(self) -> dict:
        total = self._count()
        due = self.db.execute(
            "SELECT COUNT(*) FROM srs_cards WHERE due_date<=?", (_today(),)
        ).fetchone()[0]
        return {"total": total, "due": due, "mastered": total - due}

    def _count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM srs_cards").fetchone()[0]

    def close(self):
        self.db.close()
