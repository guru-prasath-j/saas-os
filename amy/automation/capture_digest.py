"""Capture digest — the 'compare or analyze it daily/weekly' half of photo
memory.

Runs daily (evening). Compares today's captures with yesterday's (counts,
places, tags); on Sundays adds a week-over-week rollup. Writes an idempotent
digest note into 09_Memory/ — which MemoryRecall already searches — so
tomorrow's chat can lean on today's summary. LLM narrative is optional
garnish: fast + non-sensitive with a plain-stats template fallback, never a
hard dependency.
"""
from __future__ import annotations

import datetime as _dt
from collections import Counter

from .executors import JobCtx


def _stats(records: list[dict]) -> dict:
    places = Counter(r["place"] for r in records if r["place"])
    tags = Counter(t for r in records for t in r["tags"])
    return {"count": len(records),
            "places": [p for p, _ in places.most_common(5)],
            "tags": [t for t, _ in tags.most_common(8)]}


def _lines(label: str, cur: dict, prev: dict) -> list[str]:
    delta = cur["count"] - prev["count"]
    arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "=")
    out = [f"**{label}:** {cur['count']} capture(s) ({arrow}{abs(delta)} vs previous)."]
    if cur["places"]:
        new_places = [p for p in cur["places"] if p not in prev["places"]]
        out.append("Places: " + ", ".join(cur["places"])
                   + (f" (new: {', '.join(new_places)})" if new_places else ""))
    if cur["tags"]:
        out.append("Themes: " + ", ".join(cur["tags"]))
    return out


def capture_digest(ctx: JobCtx) -> dict:
    from .. import captures as captures_mod
    from ..saas import tenancy

    vault = tenancy.resolve_vault_dir(ctx.user_id)
    today = _dt.date.today()
    yday = today - _dt.timedelta(days=1)
    t_recs = captures_mod.captures_between(today.isoformat(), today.isoformat(),
                                           vault=vault)
    y_recs = captures_mod.captures_between(yday.isoformat(), yday.isoformat(),
                                           vault=vault)

    weekly = None
    if today.weekday() == 6:   # Sunday — close the week
        week_start = today - _dt.timedelta(days=6)
        prev_start = week_start - _dt.timedelta(days=7)
        prev_end = week_start - _dt.timedelta(days=1)
        weekly = (_stats(captures_mod.captures_between(
                      week_start.isoformat(), today.isoformat(), vault=vault)),
                  _stats(captures_mod.captures_between(
                      prev_start.isoformat(), prev_end.isoformat(), vault=vault)))

    if not t_recs and weekly is None:
        return {"captures_today": 0, "skipped": "nothing to digest"}

    body_lines = _lines("Today", _stats(t_recs), _stats(y_recs))
    for r in t_recs[:10]:
        piece = r["caption"] or r["note"] or (r["ocr"].splitlines()[0] if r["ocr"] else "")
        body_lines.append(f"- [[{r['path']}|{r['title']}]]"
                          + (f" — {piece}" if piece else "")
                          + (f" ({r['place']})" if r["place"] else ""))
    if weekly:
        body_lines.append("")
        body_lines += _lines("This week", weekly[0], weekly[1])

    # optional LLM narrative (fast, non-sensitive; stats above stay canonical)
    narrative = ""
    if ctx.llm is not None and t_recs:
        try:
            facts = "\n".join(body_lines)
            narrative, model = ctx.llm.generate(
                "You summarize a user's day in photos for their journal. "
                "2-3 sentences, concrete, no fluff, mention places/themes.",
                facts, sensitive=False, fast=True)
            if model == "template":
                narrative = ""
        except Exception:
            narrative = ""
    body = ((narrative.strip() + "\n\n") if narrative else "") + "\n".join(body_lines)

    from ..memory.writer import MemoryWriter
    eid = f"capdigest-{today.isoformat()}"
    p = MemoryWriter(vault).write_atomic(
        "capture-digest", f"Captures {today.isoformat()}", body, eid,
        tags=["captures", "digest"])

    try:
        ns = ctx.notify_store()
        if not ns.exists_today("capture_digest", eid):
            ns.create(type="capture_digest",
                      title=f"Photo memory digest — {today.strftime('%d %b')}",
                      body=f"{len(t_recs)} capture(s) today."
                           + (" Weekly rollup included." if weekly else ""),
                      priority="low",
                      related_entity={"entity_type": "note", "id": eid})
    except Exception:
        pass
    try:
        ctx.events().emit("capture.digest_generated",
                          {"date": today.isoformat(), "count": len(t_recs),
                           "weekly": bool(weekly)}, source="capture_digest")
    except Exception:
        pass

    return {"captures_today": len(t_recs), "captures_yesterday": len(y_recs),
            "weekly_rollup": bool(weekly), "note": str(p) if p else "already-written"}
