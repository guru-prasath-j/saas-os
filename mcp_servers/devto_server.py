"""Minimal, spec-compliant MCP server for Dev.to article search.

Wraps the official, documented, public Dev.to/Forem articles API
(developers.forem.com/api) — no API key needed for public reads. Built with
the same official `mcp` library (FastMCP) Amy's client uses
(amy/connectors/mcp.py).

Dev.to's public API is tag-based, not full-text search (there is no
documented free-text search endpoint; the one Dev.to's own frontend uses
internally is undocumented/unstable and was deliberately not used here).
search_articles() approximates a topic search by trying the query as a
single tag (spaces stripped, e.g. "generative AI" -> "generativeai"), then
falling back to each individual word as a tag, stopping at the first tag
that returns results. Works well for single-topic queries (python, react,
ai, webdev); may return nothing for narrow multi-word phrases with no
matching tag.

Run:
    python mcp_servers/devto_server.py

Then in Amy (Account -> MCP Sources -> Add source):
    Name:        Dev.to
    Server URL:  http://localhost:8004/mcp
    Auth type:   none
"""
from __future__ import annotations

import re

import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("DevTo", host="0.0.0.0", port=8004)

_ARTICLES_URL = "https://dev.to/api/articles"


def _tag_candidates(query: str) -> list[str]:
    stripped = re.sub(r"[^a-z0-9]", "", query.lower())
    words = re.findall(r"[a-z0-9]+", query.lower())
    out = []
    for c in [stripped, *words]:
        if c and c not in out:
            out.append(c)
    return out


@mcp.tool()
def search_articles(query: str, limit: int = 20) -> list[dict]:
    """Find Dev.to articles matching a topic (tag-based, see module docstring).

    Returns one dict per article: title, url, summary, score
    (public_reactions_count), published_at, tag (which candidate tag hit).
    """
    for tag in _tag_candidates(query):
        resp = requests.get(_ARTICLES_URL, params={
            "tag": tag,
            "per_page": max(1, min(limit, 50)),
        }, timeout=10)
        resp.raise_for_status()
        articles = resp.json()
        if not articles:
            continue
        return [{
            "title": a.get("title", ""),
            "url": a.get("url", ""),
            "summary": a.get("description", ""),
            "score": a.get("public_reactions_count", 0),
            "published_at": a.get("published_timestamp"),
            "tag": tag,
        } for a in articles]
    return []


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
