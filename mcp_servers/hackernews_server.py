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


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
