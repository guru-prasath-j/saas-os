"""Learning feed ranker — ONE LLM call scores all items against the user's
focus; anything unparseable degrades to the unranked list, never an error.

The llm argument is an already-constructed router (in automation jobs it's
ctx.llm, a TrackedLLM wrapping LLMRouter(use_global_keys=True)) — this
module never builds its own. generate() returns (text, provider); the
"template" provider means no real LLM was reachable, which is a fallback
signal, not a rankable response.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("amy.learning_feed")

_MAX_ITEMS = 40          # keep the single call bounded
_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)

_SYSTEM = (
    "You curate a personal learning feed. Score each numbered item for how "
    "useful it is to someone focused on the given topic. Respond with ONLY a "
    "JSON array, no prose: "
    '[{"index": <item number>, "relevance_score": <0-10>, "why": "<one short sentence>"}]'
)


def _parse_scores(text: str) -> dict[int, tuple[float, str]]:
    m = _ARRAY_RE.search(text or "")
    if not m:
        return {}
    arr = json.loads(m.group(0))
    if not isinstance(arr, list):
        return {}
    out: dict[int, tuple[float, str]] = {}
    for entry in arr:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry["index"])
            score = float(entry.get("relevance_score", 0))
        except (KeyError, TypeError, ValueError):
            continue
        out[idx] = (max(0.0, min(10.0, score)), str(entry.get("why", ""))[:300])
    return out


def rank(items: list[dict], focus: str, llm) -> list[dict]:
    """Merge relevance/why into items and sort descending. Falls back to the
    input order (relevance=None) on any LLM or parse failure."""
    if not items or llm is None:
        return items

    subset = items[:_MAX_ITEMS]
    lines = [f'{i}. [{it["source"]}] {it["title"][:120]}'
             + (f' — {it["summary"][:160]}' if it.get("summary") else "")
             for i, it in enumerate(subset)]
    prompt = (f"Focus topic: {focus}\n\nItems:\n" + "\n".join(lines)
              + "\n\nReturn the JSON array now.")

    try:
        text, provider = llm.generate(_SYSTEM, prompt, sensitive=False)
        if provider == "template":
            return items
        scores = _parse_scores(text)
    except Exception as exc:
        log.warning("learning_feed: ranking failed (%s) — returning unranked", exc)
        return items
    if not scores:
        log.warning("learning_feed: unparseable ranking response — returning unranked")
        return items

    for i, it in enumerate(subset):
        rel, why = scores.get(i, (None, ""))
        it["relevance"] = rel
        it["why"] = why
    return sorted(items, key=lambda it: it.get("relevance") or 0.0, reverse=True)
