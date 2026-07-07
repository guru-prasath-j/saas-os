"""MCP → tool-registry bridge.

Instead of registering all ~91 tools of every connected MCP server into the
catalog (which would blow up the agent prompt), the bridge exposes four
stable tools the orchestrator/assistant can compose:

  mcp_sources           read   — which MCP sources are connected
  mcp_tools             read   — a source's remote tool names/descriptions
  mcp_read              read   — call a REMOTE READ tool (name must match a
                                 read-verb pattern; anything else is refused
                                 and pointed at mcp_write)
  mcp_write             write  — call any remote tool; as a registry WRITE it
                                 parks in the Approval Inbox for agent actors,
                                 exactly like finance writes

Risk-tier guard: sources tagged scraping_backed/unofficial_risky are readable
by agents but never auto-polled aggressively here — one call per invoke, no
retries (rate-limit hygiene for scraper-backed servers like jobspy).
"""
from __future__ import annotations

import asyncio
import json
import re

from .registry import RISK_READ, RISK_WRITE, ToolError, register_tool

_READ_VERB_RE = re.compile(
    r"^(list_|get_|search_|read_|check_|semantic_|find_|fetch_)|_read$", re.I)
_RESULT_CAP = 6000   # chars of remote payload handed back to the model


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


def _user_connectors(ctx) -> list:
    from ..saas.db import SessionLocal, McpConnector
    db = SessionLocal()
    try:
        return db.query(McpConnector).filter(
            McpConnector.user_id == ctx.user_id).all()
    finally:
        db.close()


def _find_source(ctx, source: str):
    rows = _user_connectors(ctx)
    key = (source or "").strip().lower()
    for r in rows:
        if r.name.strip().lower() == key:
            return r
    for r in rows:
        if key and key in r.name.strip().lower():
            return r
    names = ", ".join(r.name for r in rows) or "none connected"
    raise ToolError(f"no MCP source matching {source!r} — connected: {names}")


def _client(row):
    from ..saas import security
    from ..connectors.mcp import MCPConnector
    auth_value = security.decrypt_secret(row.auth_ref) if row.auth_ref else None
    transport = "sse" if row.server_url.rstrip("/").endswith("/sse") else "http"
    return MCPConnector(row.server_url, auth_type=row.auth_type,
                        auth_value=auth_value, transport=transport,
                        auth_extra=row.auth_extra)


def _run(coro):
    """Tool handlers are sync (invoked from worker threads / sync routes) —
    safe to spin a private loop here."""
    return asyncio.run(coro)


def _compact(result) -> dict:
    text = json.dumps(result, default=str)
    if len(text) > _RESULT_CAP:
        return {"truncated": True, "result": text[:_RESULT_CAP]}
    return {"truncated": False, "result": result}


@register_tool("mcp_sources",
               "Connected MCP sources (name, url, risk tier, default target "
               "like owner/repo). Use the name as `source` in the other mcp_* tools.",
               _obj({}), RISK_READ)
def _t_sources(ctx, args):
    return {"sources": [{
        "name": r.name, "server_url": r.server_url, "risk_tier": r.risk_tier,
        "default_target": r.default_target,
    } for r in _user_connectors(ctx)]}


@register_tool("mcp_tools",
               "List a connected MCP source's remote tools (names + descriptions). "
               "Call before mcp_read/mcp_write so you use real tool names.",
               _obj({"source": {"type": "string"}}, ["source"]), RISK_READ)
def _t_tools(ctx, args):
    row = _find_source(ctx, args["source"])
    tools = _run(_client(row).list_tools())
    return {"source": row.name, "tools": [
        {"name": t.get("name"), "description": (t.get("description") or "")[:160]}
        for t in tools]}


@register_tool("mcp_read",
               "Call a READ tool on a connected MCP source (tool name must start "
               "with list_/get_/search_/read_/check_/semantic_/find_/fetch_). "
               "arguments is the remote tool's own JSON args — discover the "
               "schema/names via mcp_tools. For GitHub, owner/repo default to "
               "the source's default_target when omitted.",
               _obj({"source": {"type": "string"},
                     "tool": {"type": "string"},
                     "arguments": {"type": "object"}}, ["source", "tool"]), RISK_READ)
def _t_read(ctx, args):
    tool = str(args["tool"])
    if not _READ_VERB_RE.search(tool):
        raise ToolError(
            f"{tool!r} is not a read-shaped tool — use mcp_write (it will be "
            "queued for the user's approval)")
    row = _find_source(ctx, args["source"])
    call_args = dict(args.get("arguments") or {})
    if row.default_target and "/" in (row.default_target or ""):
        owner, repo = row.default_target.split("/", 1)
        call_args.setdefault("owner", owner)
        call_args.setdefault("repo", repo)
    result = _run(_client(row).call_tool(tool, call_args))
    return _compact(result)


@register_tool("mcp_write",
               "Call a WRITE tool on a connected MCP source (create/update/merge/"
               "comment/...). Agent calls are PARKED in the Approval Inbox — "
               "proposing is allowed, the human decides.",
               _obj({"source": {"type": "string"},
                     "tool": {"type": "string"},
                     "arguments": {"type": "object"}}, ["source", "tool"]), RISK_WRITE)
def _t_write(ctx, args):
    row = _find_source(ctx, args["source"])
    call_args = dict(args.get("arguments") or {})
    if row.default_target and "/" in (row.default_target or ""):
        owner, repo = row.default_target.split("/", 1)
        call_args.setdefault("owner", owner)
        call_args.setdefault("repo", repo)
    result = _run(_client(row).call_tool(str(args["tool"]), call_args))
    return _compact(result)
