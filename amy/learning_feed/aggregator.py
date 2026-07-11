"""Learning feed aggregator — fans one topic out to every promoted MCP
learning-feed connector and normalizes whatever comes back.

Uses ONLY the existing MCP connector infrastructure (_client_for →
MCPConnector.call_tool). No per-source HTTP clients: adding a new source is
a row in mcp_connectors + an entry in SOURCE_TOOLS, never a new client.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any

log = logging.getLogger("amy.learning_feed")

# case-insensitive substring of connector name → candidate MCP tool names,
# preferred first. Real community servers don't agree on naming (e.g.
# devabdultech/hn-mcp calls it plain "search"), so we list what's known to
# exist and pick the first one the server actually advertises.
SOURCE_TOOLS: list[tuple[str, tuple[str, ...]]] = [
    ("hacker news", ("search_stories", "search")),
    ("hackernews", ("search_stories", "search")),
    ("arxiv", ("search_papers",)),
    ("reddit", ("search_posts", "search_reddit", "search")),
    ("youtube", ("search_videos", "searchVideos", "search")),
    ("bluesky", ("search_posts", "search")),
    ("dev.to", ("search_articles",)),
    ("devto", ("search_articles",)),
    ("courses", ("search_courses",)),
]

# argument-name candidates for the topic, preferred first (nickytonline's
# dev-to-mcp takes q, most others take query)
_ARG_CANDIDATES = ("query", "q", "topic", "keyword", "search")

# tolerant key mapping for normalization — MCP servers for these sources
# aren't standardized, so accept the common spellings for each field.
_TITLE_KEYS = ("title", "name", "headline")
_URL_KEYS = ("url", "link", "href", "permalink", "external_url", "pdf_url")
_SUMMARY_KEYS = ("summary", "description", "abstract", "selftext", "text", "body")
_SCORE_KEYS = ("score", "points", "upvotes", "likes", "like_count")
_PUBLISHED_KEYS = ("published_at", "published", "created_at", "date", "created", "pubDate")
_LIST_KEYS = ("items", "results", "stories", "papers", "posts", "videos",
              "articles", "data", "hits", "entries",
              "result")   # FastMCP's own wrapper key for a bare list[...] return type


def tool_for(connector_name: str) -> tuple[str, ...] | None:
    """Candidate MCP tool names by case-insensitive substring match on the
    connector's registered name. None = not a learning-feed source."""
    low = (connector_name or "").lower()
    for needle, tools in SOURCE_TOOLS:
        if needle in low:
            return tools
    return None


def source_label(connector_name: str) -> str:
    low = (connector_name or "").lower()
    for needle, _tool in SOURCE_TOOLS:
        if needle in low:
            return needle.replace(" ", "").replace(".", "")
    return (connector_name or "unknown").strip().lower()


def _first(d: dict, keys: tuple[str, ...]):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


# numbered-text fallback: some servers (e.g. devabdultech/hn-mcp) return a
# formatted listing, not JSON:
#   1. Title of the story
#      ID: 36972347
#      URL: https://...
#      Points: 906 | Author: x | Comments: 319
_ITEM_HEAD_RE = re.compile(r"(?m)^\s*\d+\.\s+(.+?)\s*$")
_URL_RE = re.compile(r"https?://[^\s)>\"']+")
_POINTS_RE = re.compile(r"(?:points|score|upvotes|likes)\s*[:=]\s*(\d+)", re.I)


def _parse_text_blocks(text: str) -> list[dict]:
    heads = list(_ITEM_HEAD_RE.finditer(text))
    items = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[m.end():end]           # lines under the numbered head
        url = _URL_RE.search(body)
        if not url:
            continue
        pts = _POINTS_RE.search(body)
        items.append({"title": m.group(1), "url": url.group(0),
                      "score": int(pts.group(1)) if pts else 0})
    return items


def _extract_items(result: dict) -> list[dict]:
    """call_tool returns {"is_error", "text", "structured"} — try the
    structured payload first, then JSON embedded in the text blocks, then
    the numbered-plain-text fallback."""
    for candidate in (result.get("structured"), result.get("text")):
        if candidate is None:
            continue
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except Exception:
                continue
        if isinstance(candidate, list):
            return [x for x in candidate if isinstance(x, dict)]
        if isinstance(candidate, dict):
            for k in _LIST_KEYS:
                v = candidate.get(k)
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]
    text = result.get("text")
    if isinstance(text, str) and text.strip():
        return _parse_text_blocks(text)
    return []


def _normalize(raw: dict, source: str) -> dict | None:
    title = _first(raw, _TITLE_KEYS)
    url = _first(raw, _URL_KEYS)
    if not url:
        # YouTube API shapes carry a video id, not a URL — build the watch link
        vid = raw.get("videoId") or raw.get("video_id")
        if not vid and isinstance(raw.get("id"), dict):     # raw API: id.videoId
            vid = raw["id"].get("videoId")
        if vid:
            url = f"https://www.youtube.com/watch?v={vid}"
    if not title or not url:
        return None
    title, url = str(title).strip(), str(url).strip()
    summary = str(_first(raw, _SUMMARY_KEYS) or "").strip()[:1000]
    try:
        score = int(float(_first(raw, _SCORE_KEYS) or 0))
    except (TypeError, ValueError):
        score = 0
    published = _first(raw, _PUBLISHED_KEYS)
    return {
        # deterministic per-URL so a re-fetch upserts the same row and the
        # user's saved flag survives refreshes
        "id": hashlib.sha1(url.encode("utf-8")).hexdigest()[:16],
        "source": source,
        "title": title[:300],
        "url": url[:1000],
        "summary": summary,
        "score": score,
        "published_at": str(published) if published is not None else None,
    }


async def _fetch_one(row, candidates: tuple[str, ...], topic: str) -> list[dict]:
    # Lazy import: the router module imports SaaS deps; importing it at
    # module load from library code would be circular.
    from ..saas.routers.mcp_connectors import _client_for
    client = _client_for(row)

    # Ask the server what it actually has: pick the first known candidate
    # tool, and read the topic argument's name from its schema (query vs q).
    advertised = await client.list_tools()
    by_name = {t["name"]: t for t in advertised}
    tool = next((c for c in candidates if c in by_name), None)
    if tool is None:
        raise RuntimeError(
            f"server has none of {list(candidates)} (tools: {sorted(by_name)[:10]})")
    props = (by_name[tool].get("input_schema") or {}).get("properties") or {}
    arg = next((a for a in _ARG_CANDIDATES if a in props), _ARG_CANDIDATES[0])

    result = await client.call_tool(tool, {arg: topic})
    if result.get("is_error"):
        raise RuntimeError(f"tool {tool} returned is_error: {result.get('text', '')[:200]}")
    return _extract_items(result)


async def fetch_all(topic: str, connector_rows: list) -> list[dict]:
    """Call every matching connector concurrently; one failing source never
    crashes the feed. Returns normalized, URL-deduped items."""
    targets = []
    for row in connector_rows or []:
        tool = tool_for(row.name)
        if tool is None:
            log.warning("learning_feed: connector %r matches no known source — skipped", row.name)
            continue
        targets.append((row, tool))

    if not targets:
        log.warning("learning_feed: no promoted learning-feed MCP connectors registered — empty feed")
        return []

    results = await asyncio.gather(
        *(_fetch_one(row, tool, topic) for row, tool in targets),
        return_exceptions=True)

    items: list[dict] = []
    seen_urls: set[str] = set()
    for (row, tool), res in zip(targets, results):
        if isinstance(res, BaseException):
            from ..connectors.mcp import describe_error
            log.warning("learning_feed: %r (%s) failed: %s", row.name, tool, describe_error(res))
            continue
        src = source_label(row.name)
        for raw in res:
            item = _normalize(raw, src)
            if item is None or item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            items.append(item)
    return items
