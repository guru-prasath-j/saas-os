"""Minimal, spec-compliant MCP server for free-course search.

Two REAL sources, no scraping and no LLM anywhere:
  (a) freeCodeCamp — the official curriculum-data JSON feed
      (freecodecamp.org/curriculum-data, the same static data their mobile
      app consumes); matched entries link to freecodecamp.org/learn paths.
  (b) Microsoft Learn — the OFFICIAL catalog API
      (learn.microsoft.com/api/catalog/), filtered client-side.

Google Cloud Skills Boost is DELIBERATELY OMITTED: it has no public
catalog API, and scraping its pages is brittle + ToS-risky — same repo
rule that kept Naukri scraping workarounds out of the job scout.

Each source is fetched in its own try/except (one failing only shrinks
results) and cached in-process for 24h (module-level dict, same idiom as
the zakat gold-price cache) so the learning feed's 6h polls don't
re-download the catalogs.

Run:
    python mcp_servers/courses_server.py

Then in Amy (Account -> MCP Sources -> Add source):
    Name:        Courses
    Server URL:  http://localhost:8005/mcp
    Auth type:   none
"""
from __future__ import annotations

import re
import sys
import time

import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Courses", host="0.0.0.0", port=8005)

_FCC_URL = ("https://raw.githubusercontent.com/freeCodeCamp/freeCodeCamp/"
            "main/curriculum/structure/curriculum.json")
_MSLEARN_URL = ("https://learn.microsoft.com/api/catalog/"
                "?locale=en-us&type=learningPaths,courses")

_CACHE_TTL_SECONDS = 24 * 3600
_cache: dict[str, tuple[float, object]] = {}

_STOPWORDS = {"the", "and", "for", "with", "into", "from", "course",
              "courses", "learn", "learning", "jobs", "engineer",
              "engineering", "developer"}


def _cached_get_json(key: str, url: str):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    resp = requests.get(url, timeout=30,
                        headers={"Accept": "application/json"})
    resp.raise_for_status()
    data = resp.json()
    _cache[key] = (now, data)
    return data


def _tokens(query: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9+#.]+", (query or "").lower())
            if len(t) >= 2 and t not in _STOPWORDS]


def _score(text: str, tokens: list[str]) -> int:
    """Distinct WHOLE-WORD matches — substring matching made 'rag' hit
    'storage' (found while smoke-testing), whole words don't."""
    words = set(re.findall(r"[a-z0-9+#.]+", (text or "").lower()))
    return sum(1 for t in tokens if t in words)


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _fcc_items(tokens: list[str]) -> list[dict]:
    data = _cached_get_json("fcc", _FCC_URL)
    raw = data.get("superblocks") if isinstance(data, dict) else data
    items = []
    for sb in raw or []:
        if isinstance(sb, dict):
            dashed = str(sb.get("dashedName") or sb.get("slug") or "")
            title = str(sb.get("title") or dashed.replace("-", " ").title())
        else:
            dashed = str(sb)
            title = dashed.replace("-", " ").title()
        if not dashed:
            continue
        score = _score(title, tokens)
        if score <= 0:
            continue
        items.append({
            "title": f"{title} (freeCodeCamp)",
            "url": f"https://www.freecodecamp.org/learn/{dashed}/",
            "summary": "Free certification/curriculum track on freeCodeCamp.",
            "score": score,
            "_distinct": score,
            "published_at": None,
        })
    return items


def _mslearn_items(tokens: list[str], limit: int) -> list[dict]:
    data = _cached_get_json("mslearn", _MSLEARN_URL)
    pool = []
    if isinstance(data, dict):
        pool = list(data.get("learningPaths") or []) + list(data.get("courses") or [])
    items = []
    for c in pool:
        if not isinstance(c, dict):
            continue
        title = str(c.get("title") or "")
        summary = _strip_html(str(c.get("summary") or ""))
        products = " ".join(str(p) for p in (c.get("products") or []))
        # distinct token coverage gates relevance; the title-weighted score
        # only ranks among items that passed
        distinct = _score(f"{title} {summary} {products}", tokens)
        score = (2 * _score(title, tokens) + _score(summary, tokens)
                 + _score(products, tokens))
        if distinct <= 0 or not c.get("url"):
            continue
        duration = c.get("duration_in_minutes")
        levels = ", ".join(str(x) for x in (c.get("levels") or []))
        detail_bits = [b for b in
                       (f"{duration} min" if duration else "", levels) if b]
        detail = f" [{' · '.join(detail_bits)}]" if detail_bits else ""
        items.append({
            "title": f"{title} (Microsoft Learn)",
            "url": str(c.get("url")),
            "summary": (summary[:300] + detail) or "Microsoft Learn path.",
            "score": score,
            "_distinct": distinct,
            "published_at": c.get("last_modified"),
        })
        if len(items) >= limit * 4:
            break   # plenty to rank from; keep the response bounded
    return items


@mcp.tool()
def search_courses(query: str, limit: int = 20) -> list[dict]:
    """Search free courses/learning paths matching a topic.

    Sources: freeCodeCamp curriculum feed + Microsoft Learn official
    catalog API (see module docstring — no scraping, no fabrication).
    Returns one dict per course: title (with source site), url, summary,
    score (keyword-match strength), published_at (null for evergreen
    tracks).
    """
    tokens = _tokens(query)
    if not tokens:
        return []
    items: list[dict] = []
    for fetch in (lambda: _fcc_items(tokens),
                  lambda: _mslearn_items(tokens, limit)):
        try:
            items.extend(fetch())
        except Exception as exc:
            print(f"courses: source failed, continuing: {exc}", file=sys.stderr)
    # multi-token queries must match at least two distinct tokens — a
    # single generic word ('databases') pulls in every DB course otherwise
    min_distinct = min(2, len(tokens))
    items = [i for i in items if i.pop("_distinct", 1) >= min_distinct]
    items.sort(key=lambda x: -x["score"])
    return items[:max(1, min(limit, 50))]


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
