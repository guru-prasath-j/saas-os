"""MemoryWriter — the Journaler (Operational layer).

Writes events into the Obsidian vault as markdown:
  * a per-day daily note  : 00_Daily/YYYY-MM-DD.md   (timestamped append log)
  * atomic memory notes   : 09_Memory/<Type> - <slug>.md  (for significant items)

Design guarantees
-----------------
* **Vault-as-truth** — markdown is canonical; this writer never depends on SQLite.
* **Idempotent** — every daily entry carries an HTML-comment marker
  ``<!-- eid:<event_id> -->``. Before appending, the writer checks the day's note
  for that marker, so re-imports / cloud-sync re-reads never duplicate. Atomic
  notes are keyed by a deterministic filename and skipped if already present.
* **Self-contained** — only stdlib + the vault path. No engine, no DB.

Phase 3: an optional EntityIndex auto-links each entry to known vault notes/goals.
"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

DAILY_DIR = "00_Daily"
MEMORY_DIR = "09_Memory"

# event.type -> (human label, whether it also becomes an atomic note)
_KIND = {
    "query.asked": ("chat", False),
    "decision.recorded": ("decision", True),
    "decision.resolved": ("decision", False),
    "capture.added": ("capture", True),
    "goal.created": ("goal", True),
    "goal.completed": ("goal", False),
    "digest.generated": ("digest", False),
    "agent.toggled": ("system", False),
    "vault.imported": ("system", False),
    "vault.note_edited": ("vault", False),
    # Finance events
    "finance.transaction_added": ("finance", False),
    "finance.csv_imported": ("finance", True),
    "finance.pdf_imported": ("finance", True),
    "finance.gmail_synced": ("finance", True),
    "finance.budget_set": ("finance", False),
    "finance.subscription_added": ("finance", False),
    "finance.investment_added": ("finance", False),
}


def _slug(text: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", (text or "").strip()).strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:maxlen]).strip() or "note"


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class MemoryWriter:
    def __init__(self, vault_path, entity_index=None):
        self.vault = Path(vault_path)
        # optional EntityIndex (Phase 3): auto-links entries to known notes/goals
        self.entities = entity_index

    # --- paths ----------------------------------------------------------
    def _daily_path(self, date: _dt.date) -> Path:
        return self.vault / DAILY_DIR / f"{date.isoformat()}.md"

    def _memory_path(self, type_label: str, title: str) -> Path:
        fname = f"{type_label.capitalize()} - {_slug(title)}.md"
        return self.vault / MEMORY_DIR / fname

    # --- helpers --------------------------------------------------------
    @staticmethod
    def _has_marker(text: str, eid: str) -> bool:
        return f"<!-- eid:{eid} -->" in text

    def _ensure_daily(self, path: Path, date: _dt.date) -> str:
        if path.exists():
            return path.read_text(encoding="utf-8", errors="ignore")
        header = (f"---\ntype: daily\ndate: {date.isoformat()}\n---\n"
                  f"# {date.isoformat()}\n\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header, encoding="utf-8")
        return header

    # --- core writes ----------------------------------------------------
    def append_daily(self, kind: str, text: str, eid: str,
                     when: _dt.datetime | None = None,
                     links: list[str] | None = None,
                     tags: list[str] | None = None) -> bool:
        """Append one timestamped entry to today's daily note. Idempotent on eid.
        Returns True if written, False if it was already there (skipped)."""
        when = when or _now()
        date = when.date()
        path = self._daily_path(date)
        current = self._ensure_daily(path, date)
        if self._has_marker(current, eid):
            return False
        link_str = " " + " ".join(f"[[{l}]]" for l in (links or [])) if links else ""
        tag_str = " " + " ".join(f"#{t}" for t in (tags or [])) if tags else ""
        entry = (f"\n## {when.strftime('%H:%M')} — {kind} <!-- eid:{eid} -->\n"
                 f"{text}{link_str}{tag_str}\n")
        with path.open("a", encoding="utf-8") as f:
            f.write(entry)
        return True

    def write_atomic(self, type_label: str, title: str, body: str, eid: str,
                     links: list[str] | None = None, tags: list[str] | None = None,
                     when: _dt.datetime | None = None) -> Path | None:
        """Create an atomic memory note. Skips if a note with this eid exists.
        Returns the path written, or None if skipped."""
        when = when or _now()
        path = self._memory_path(type_label, title)
        if path.exists():
            existing = path.read_text(encoding="utf-8", errors="ignore")
            if self._has_marker(existing, eid):
                return None
            # filename collision with a *different* event — disambiguate
            path = path.with_name(path.stem + f" ({eid}).md")
        fm = ["---", f"type: {type_label}", f"date: {when.date().isoformat()}",
              f"created: {when.isoformat()}"]
        if tags:
            fm.append("tags: [" + ", ".join(tags) + "]")
        fm.append("---")
        link_block = ""
        if links:
            link_block = "\n\n## Links\n" + "\n".join(f"- [[{l}]]" for l in links)
        content = (f"{chr(10).join(fm)}\n\n# {title}\n\n{body}{link_block}\n"
                   f"\n<!-- eid:{eid} -->\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    # --- event dispatch -------------------------------------------------
    def log_event(self, event: dict) -> dict:
        """Journal one event dict ({id,type,payload,ts,source}). Returns what was
        written: {'daily': bool, 'atomic': str|None}."""
        eid = event.get("id") or ""
        etype = event.get("type", "")
        payload = event.get("payload") or {}
        when = self._parse_ts(event.get("ts"))
        label, make_atomic = _KIND.get(etype, ("event", False))
        # github.* events
        if etype.startswith("github."):
            label, make_atomic = "github", etype in ("github.NEW_REPOSITORY",
                                                      "github.NEW_RELEASE")
        daily_text, title, body, links, tags = self._format(etype, label, payload)
        # Phase 3: enrich with auto-detected entity links/tags from the text
        if self.entities is not None:
            extra_links, extra_tags = self.entities.extract(f"{daily_text} {title or ''} {body}")
            links = list(dict.fromkeys(links + extra_links))
            tags = list(dict.fromkeys(tags + extra_tags))
        wrote_daily = self.append_daily(label, daily_text, eid, when=when,
                                        links=links, tags=tags)
        atomic_path = None
        if make_atomic and title:
            p = self.write_atomic(label, title, body, eid, links=links,
                                  tags=tags, when=when)
            atomic_path = str(p.relative_to(self.vault)) if p else None
        return {"daily": wrote_daily, "atomic": atomic_path}

    # --- formatting per event type -------------------------------------
    def _format(self, etype: str, label: str, p: dict):
        """Return (daily_text, atomic_title, atomic_body, links, tags)."""
        tags = [label]
        if etype == "query.asked":
            q = p.get("query") or p.get("text") or ""
            a = p.get("answer") or ""
            txt = f"**Q:** {q}" + (f"\n\n**A:** {a[:500]}" if a else "")
            return txt, None, "", [], tags
        if etype == "decision.recorded":
            title = p.get("title", "decision")
            cat = p.get("category") or p.get("domain") or "personal"
            conf = p.get("confidence")
            txt = f"Decision: **{title}** ({cat}" + (f", confidence {conf}" if conf is not None else "") + ")"
            body = f"- Category: {cat}\n- Confidence: {conf}\n- Reason: {p.get('reason','')}"
            return txt, title, body, [cat.capitalize()], [label, cat]
        if etype == "decision.resolved":
            return f"Decision resolved: {p.get('id','')} → {p.get('status','')}", None, "", [], tags
        if etype == "capture.added":
            title = p.get("title") or "capture"
            txt = f"Capture: **{title}**" + (f" — {p.get('caption','')}" if p.get("caption") else "")
            body = p.get("caption", "") or p.get("note", "")
            return txt, title, body, [], tags
        if etype == "goal.created":
            title = p.get("title", "goal")
            return f"New goal: **{title}**", title, f"Domain: {p.get('domain','')}", [], tags
        if etype == "goal.completed":
            return f"Goal completed: **{p.get('title','')}** 🎉", None, "", [], tags
        if etype.startswith("github."):
            repo = p.get("repo", "")
            t = p.get("title", "")
            kind = etype.split(".", 1)[1].replace("_", " ").lower()
            txt = f"GitHub {kind} in `{repo}`: {t}"
            links = [repo.split("/")[-1]] if repo else []
            return txt, (f"{t} ({repo})" if t else repo), f"Repo: {repo}\nURL: {p.get('url','')}", links, ["github"]
        if etype == "digest.generated":
            return "Daily digest generated.", None, "", [], tags
        if etype == "vault.note_edited":
            path = p.get("path", "")
            return f"Vault note edited: `{path}`", None, "", [], ["vault"]
        # Finance events
        if etype == "finance.transaction_added":
            amt = p.get("amount", 0)
            merchant = p.get("merchant", "")
            cat = p.get("category", "")
            sign = "+" if amt > 0 else ""
            return (f"Transaction: {merchant} {sign}₹{abs(amt):,.0f} [{cat}]",
                    None, "", [], ["finance"])
        if etype == "finance.csv_imported":
            bank = p.get("bank_name", "bank")
            n = p.get("imported", 0)
            skipped = p.get("skipped", 0)
            title = f"CSV import — {bank} ({n} transactions)"
            body = f"- Bank: {bank}\n- Imported: {n}\n- Skipped: {skipped}"
            return (f"Imported {n} transactions from {bank} CSV (skipped {skipped})",
                    title, body, [bank], ["finance", "import"])
        if etype == "finance.pdf_imported":
            bank = p.get("bank_name", "bank")
            n = p.get("imported", 0)
            skipped = p.get("skipped", 0)
            title = f"PDF import — {bank} ({n} transactions)"
            body = f"- Bank: {bank}\n- Imported: {n}\n- Skipped: {skipped}"
            return (f"Imported {n} transactions from {bank} PDF (skipped {skipped})",
                    title, body, [bank], ["finance", "import"])
        if etype == "finance.gmail_synced":
            n = p.get("imported", 0)
            skipped = p.get("skipped", 0)
            accounts = p.get("accounts_synced", 0)
            title = f"Gmail sync — {n} transactions"
            body = (f"- Imported: {n}\n- Skipped: {skipped}\n"
                    f"- Accounts synced: {accounts}")
            return (f"Gmail sync: {n} new transactions (skipped {skipped})",
                    title, body, [], ["finance", "gmail"])
        if etype == "finance.budget_set":
            cat = p.get("category", "")
            limit = p.get("monthly_limit", 0)
            return (f"Budget set: {cat} → ₹{limit:,.0f}/month",
                    None, "", [], ["finance", "budget"])
        if etype == "finance.subscription_added":
            name = p.get("name", "")
            cost = p.get("monthly_cost", 0)
            return (f"Subscription added: {name} (₹{cost:,.0f}/month)",
                    None, "", [], ["finance", "subscription"])
        if etype == "finance.investment_added":
            name = p.get("name", "")
            itype = p.get("type", "")
            value = p.get("current_value", 0)
            return (f"Investment added: {name} ({itype}, ₹{value:,.0f})",
                    None, "", [], ["finance", "investment"])
        # generic
        summary = ", ".join(f"{k}={v}" for k, v in list(p.items())[:4])
        return f"{etype}: {summary}", None, "", [], tags

    @staticmethod
    def _parse_ts(ts):
        if not ts:
            return None
        try:
            return _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            return None
