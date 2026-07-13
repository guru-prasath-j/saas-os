"""Minimal, spec-compliant MCP server for Hacker News search.

Uses the free, public Algolia HN Search API (hn.algolia.com) — no API key,
no scraping. Built with the same official `mcp` library (FastMCP) Amy's
client uses (amy/connectors/mcp.py), so it speaks the real MCP protocol
correctly by construction, unlike some community HN MCP forks that expose
non-standard routes.

Run:
    python mcp_servers/hackernews_server.py

Then in Amy (Account -> MCP Sources -> Add source):
    Name:        HackerNews
    Server URL:  http://localhost:8001/mcp
    Auth type:   none
"""
from __future__ import annotations

import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("HackerNews", host="0.0.0.0", port=8001)

_SEARCH_URL = "https://hn.algolia.com/api/v1/search"


@mcp.tool()
def search_stories(query: str, limit: int = 20) -> list[dict]:
    """Search Hacker News stories matching a query, newest-relevant first.

    Returns one dict per story: title, url (falls back to the HN discussion
    link for text-only posts like Ask/Show HN), points, created_at, summary.
    """
    resp = requests.get(_SEARCH_URL, params={
        "query": query,
        "tags": "story",
        "hitsPerPage": max(1, min(limit, 50)),
    }, timeout=10)
    resp.raise_for_status()
    hits = resp.json().get("hits", [])
    out = []
    for h in hits:
        object_id = h.get("objectID")
        out.append({
            "title": h.get("title") or h.get("story_title") or "",
            "url": h.get("url") or h.get("story_url")
                   or (f"https://news.ycombinator.com/item?id={object_id}" if object_id else ""),
            "points": h.get("points") or 0,
            "created_at": h.get("created_at"),
            "summary": (h.get("story_text") or "")[:500],
        })
    return out


@mcp.tool()
def whos_hiring(query: str = "", limit: int = 30) -> list[dict]:
    """Search the most recent monthly "Ask HN: Who is Hiring?" thread's
    top-level hiring comments (each comment is one job posting), via the
    same public Algolia HN Search API — no scraping, no new dependency.

    First finds the latest "Who is Hiring" story authored by the
    official "whoishiring" account, then searches WITHIN that story's
    comments (Algolia's tags=comment,story_<id> shape), optionally
    filtered by `query` (e.g. a skill keyword). Returns one dict per
    comment: title (first ~120 chars, a pseudo-title — comments have no
    structured company/role fields), url, summary (full comment text,
    capped), points, created_at, author.
    """
    story_resp = requests.get(_SEARCH_URL, params={
        "query": "Who is Hiring",
        "tags": "story",
        "hitsPerPage": 10,
    }, timeout=10)
    story_resp.raise_for_status()
    hits = story_resp.json().get("hits", [])
    story_id = None
    for h in sorted(hits, key=lambda h: h.get("created_at") or "", reverse=True):
        title = str(h.get("title") or "")
        if h.get("author") == "whoishiring" and "who is hiring" in title.lower():
            story_id = h.get("objectID")
            break
    if story_id is None:
        return []

    params = {"tags": f"comment,story_{story_id}", "hitsPerPage": max(1, min(limit, 50))}
    if query:
        params["query"] = query
    comment_resp = requests.get(_SEARCH_URL, params=params, timeout=10)
    comment_resp.raise_for_status()
    out = []
    for h in comment_resp.json().get("hits", []):
        text = (h.get("comment_text") or "").strip()
        if not text:
            continue
        object_id = h.get("objectID")
        out.append({
            "title": text[:120],
            "url": f"https://news.ycombinator.com/item?id={object_id}" if object_id else "",
            "summary": text[:1500],
            "points": h.get("points") or 0,
            "created_at": h.get("created_at"),
            "author": h.get("author"),
        })
    return out


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
