"""Consolidator (Phase 5) — the Learning layer.

Reads the daily notes the Journaler produced and rolls them up into a weekly
summary note written back into the vault (01_Weekly/YYYY-Www.md). This keeps the
memory lake navigable as it grows infinitely: instead of scrolling 7 daily logs,
you get one digest with counts, the week's decisions, new goals, and the topics
(tags/links) you touched most.

Vault-as-truth & offline: it parses the daily markdown directly (no DB, no LLM),
so it works on whatever is in the vault — including notes you wrote by hand in
Obsidian. Re-running overwrites the derived weekly note (it's a pure function of
the dailies), so it's safe to schedule.

`patterns()` exposes the same aggregates as data, so other engines (personality,
decision) can consume the learning signal.
"""
from __future__ import annotations

import datetime as _dt
import re
from collections import Counter
from pathlib import Path

from .writer import DAILY_DIR

WEEKLY_DIR = "01_Weekly"

_ENTRY = re.compile(r"^##\s+\d{2}:\d{2}\s+—\s+(\w+)\s+<!--\s*eid:", re.M)
_TAG = re.compile(r"#(\w+)")
_LINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
_DECISION = re.compile(r"Decision:\s+\*\*(.+?)\*\*", re.M)
_GOAL = re.compile(r"New goal:\s+\*\*(.+?)\*\*", re.M)


def _week_label(d: _dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _week_dates(d: _dt.date) -> list[_dt.date]:
    monday = d - _dt.timedelta(days=d.weekday())
    return [monday + _dt.timedelta(days=i) for i in range(7)]


class Consolidator:
    def __init__(self, vault_path):
        self.vault = Path(vault_path)

    # --- read dailies ---------------------------------------------------
    def _daily_text(self, day: _dt.date) -> str:
        p = self.vault / DAILY_DIR / f"{day.isoformat()}.md"
        return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else ""

    # --- aggregation ----------------------------------------------------
    def patterns(self, ref: _dt.date | None = None) -> dict:
        """Aggregate the week's activity into a data dict."""
        ref = ref or _dt.datetime.now(_dt.timezone.utc).date()
        days = _week_dates(ref)
        kinds: Counter = Counter()
        tags: Counter = Counter()
        links: Counter = Counter()
        decisions: list[str] = []
        goals: list[str] = []
        active_days = 0
        for day in days:
            text = self._daily_text(day)
            if not text:
                continue
            entries = _ENTRY.findall(text)
            if entries:
                active_days += 1
            kinds.update(entries)
            tags.update(_TAG.findall(text))
            links.update(_LINK.findall(text))
            decisions += _DECISION.findall(text)
            goals += _GOAL.findall(text)
        return {
            "week": _week_label(ref),
            "range": [days[0].isoformat(), days[-1].isoformat()],
            "active_days": active_days,
            "total_entries": sum(kinds.values()),
            "by_kind": dict(kinds.most_common()),
            "top_tags": [t for t, _ in tags.most_common(8)],
            "top_links": [l for l, _ in links.most_common(8)],
            "decisions": decisions,
            "new_goals": goals,
        }

    # --- write weekly note ---------------------------------------------
    def weekly(self, ref: _dt.date | None = None) -> dict:
        """Build (or refresh) the weekly rollup note. Returns {path, patterns}."""
        p = self.patterns(ref)
        if p["total_entries"] == 0:
            return {"path": None, "patterns": p, "written": False}
        body = self._render(p)
        path = self.vault / WEEKLY_DIR / f"{p['week']}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return {"path": str(path.relative_to(self.vault)), "patterns": p, "written": True}

    @staticmethod
    def _render(p: dict) -> str:
        kinds = ", ".join(f"{k}×{v}" for k, v in p["by_kind"].items()) or "—"
        decisions = "\n".join(f"- {d}" for d in p["decisions"]) or "- none"
        goals = "\n".join(f"- {g}" for g in p["new_goals"]) or "- none"
        tags = " ".join(f"#{t}" for t in p["top_tags"]) or "—"
        links = " ".join(f"[[{l}]]" for l in p["top_links"]) or "—"
        return (
            f"---\ntype: weekly\nweek: {p['week']}\n"
            f"range: {p['range'][0]} → {p['range'][1]}\n---\n\n"
            f"# Week {p['week']}\n\n"
            f"Active days: **{p['active_days']}/7** · Entries: **{p['total_entries']}**\n\n"
            f"## Activity\n{kinds}\n\n"
            f"## Decisions\n{decisions}\n\n"
            f"## New goals\n{goals}\n\n"
            f"## Topics\nTags: {tags}\n\nMost-linked: {links}\n"
        )
