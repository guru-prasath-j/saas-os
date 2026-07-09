"""Minimal, spec-compliant MCP server for YouTube video search.

Wraps the real YouTube Data API v3 (search.list) — requires a free API key
from Google Cloud Console (Library -> enable "YouTube Data API v3" ->
Credentials -> Create API key), set as YOUTUBE_API_KEY. Built with the same
official `mcp` library (FastMCP) Amy's client uses (amy/connectors/mcp.py).

Run:
    YOUTUBE_API_KEY=... python mcp_servers/youtube_server.py

Then in Amy (Account -> MCP Sources -> Add source):
    Name:        YouTube
    Server URL:  http://localhost:8003/mcp
    Auth type:   none
"""
from __future__ import annotations

import os

import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("YouTube", host="0.0.0.0", port=8003)

_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"


@mcp.tool()
def search_videos(query: str, limit: int = 20) -> list[dict]:
    """Search YouTube videos matching a query.

    Returns one dict per video: title, videoId (the caller builds the watch
    URL from this), summary, published_at, channel. Returns [] rather than
    raising when YOUTUBE_API_KEY isn't set, so a missing key degrades this
    one source instead of breaking the whole Learning Feed fetch.
    """
    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key:
        return []
    resp = requests.get(_SEARCH_URL, params={
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": max(1, min(limit, 25)),
        "key": api_key,
    }, timeout=10)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    out = []
    for it in items:
        video_id = (it.get("id") or {}).get("videoId")
        if not video_id:
            continue
        snippet = it.get("snippet") or {}
        out.append({
            "title": snippet.get("title", ""),
            "videoId": video_id,
            "summary": snippet.get("description", ""),
            "published_at": snippet.get("publishedAt"),
            "channel": snippet.get("channelTitle", ""),
        })
    return out


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
