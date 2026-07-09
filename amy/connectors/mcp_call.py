"""Shared "call a named capability on a registered MCP connector" helper.

Real MCP servers for the same capability don't agree on tool/arg names (see
amy/learning_feed/aggregator.py's SOURCE_TOOLS comment for the canonical
explanation) so every call here: (1) lists the connector's advertised
tools, (2) picks the first candidate name that's actually present, (3)
fills the connector's default_target into the call args when the remote
schema has room for it, (4) logs the attempt to connector_calls regardless
of outcome — feeds the Part 3 connectors health tab and the audit report's
external-write governance count.

Used by both amy/tools/connector_tools.py (GitHub/Plane READ tools — the
registry handler calls this directly) and amy/automation/executors.py
(GitHub/Plane WRITE executors — the approved-action code path). Kept
dependency-light (amy.connectors.mcp, amy.saas.db, amy.saas.security only)
so importing it from either side never risks a cycle back through
amy.tools/amy.automation.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any


class ConnectorCallError(RuntimeError):
    """Connector unreachable, misconfigured, or the remote tool errored —
    message is safe to surface to the model/user (no secrets)."""


def find_connector_row(user_id: str, name_substring: str):
    """First McpConnector row for this user whose name contains
    name_substring (case-insensitive) — same matching rule as
    amy/tools/mcp_bridge.py's _find_source, minus the ToolError coupling."""
    from ..saas.db import SessionLocal, McpConnector
    s = SessionLocal()
    try:
        rows = s.query(McpConnector).filter(McpConnector.user_id == user_id).all()
    finally:
        s.close()
    key = name_substring.strip().lower()
    for r in rows:
        if r.name.strip().lower() == key:
            return r
    for r in rows:
        if key in r.name.strip().lower():
            return r
    return None


def mcp_client_for(row):
    from ..saas import security
    from .mcp import MCPConnector
    auth_value = security.decrypt_secret(row.auth_ref) if row.auth_ref else None
    transport = "sse" if row.server_url.rstrip("/").endswith("/sse") else "http"
    return MCPConnector(row.server_url, auth_type=row.auth_type,
                        auth_value=auth_value, transport=transport,
                        auth_extra=row.auth_extra)


def _run(coro):
    """Tool/executor handlers are sync — safe to spin a private loop here
    (same stance as amy/tools/mcp_bridge.py's _run)."""
    return asyncio.run(coro)


_RESULT_CAP = 6000


def _compact(result: dict) -> dict:
    import json
    text = json.dumps(result, default=str)
    if len(text) > _RESULT_CAP:
        return {"truncated": True, "result": text[:_RESULT_CAP]}
    return {"truncated": False, "result": result}


def call_mcp_tool(user_id: str, store, source: str, candidates: tuple[str, ...],
                  args: dict, target_style: str = "owner_repo") -> dict[str, Any]:
    """Resolve `source` to a connector, call the first candidate tool name
    it advertises, log the attempt, return the compacted result.

    target_style: how to fold row.default_target into call args when the
    caller didn't already supply the equivalent key —
      "owner_repo": default_target is "owner/repo" (GitHub) — sets
                    owner/repo if the remote schema has room for them.
      "single":     default_target is a single id (e.g. Plane project id) —
                    set on the first schema property matching a small set
                    of common id-ish names.
      "none":       never inject anything from default_target.

    store: AutomationStore (for connector_calls logging) — pass ctx.store.
    Raises ConnectorCallError on any failure (no connector found, none of
    the candidate tools advertised, or the remote call errors).
    """
    row = find_connector_row(user_id, source)
    if row is None:
        raise ConnectorCallError(
            f"no {source!r} MCP connector registered — add one in "
            "Account -> MCP Sources")

    t0 = time.monotonic()
    tool_used = candidates[0] if candidates else "?"
    try:
        client = mcp_client_for(row)
        advertised = _run(client.list_tools())
        by_name = {t["name"]: t for t in advertised}
        tool_used = next((c for c in candidates if c in by_name), None)
        if tool_used is None:
            raise ConnectorCallError(
                f"{source} server has none of {list(candidates)} "
                f"(advertised: {sorted(by_name)[:12]})")

        call_args = dict(args)
        target = (row.default_target or "").strip()
        if target_style == "owner_repo" and target and "/" in target:
            owner, repo = target.split("/", 1)
            call_args.setdefault("owner", owner)
            call_args.setdefault("repo", repo)
        elif target_style == "single" and target:
            props = (by_name[tool_used].get("input_schema") or {}).get("properties") or {}
            for cand in ("project_id", "project", "projectId", "workspace_slug"):
                if cand in props and cand not in call_args:
                    call_args[cand] = target
                    break

        result = _run(client.call_tool(tool_used, call_args))
        ms = int((time.monotonic() - t0) * 1000)
        is_error = bool(result.get("is_error"))
        _log(store, user_id, source, tool_used, not is_error, ms,
             result.get("text", "")[:300] if is_error else "")
        if is_error:
            raise ConnectorCallError(
                f"{source}.{tool_used} returned an error: {result.get('text', '')[:300]}")
        return _compact(result)
    except ConnectorCallError:
        raise
    except Exception as exc:
        from .mcp import describe_error
        ms = int((time.monotonic() - t0) * 1000)
        msg = describe_error(exc)
        _log(store, user_id, source, tool_used, False, ms, msg[:300])
        raise ConnectorCallError(f"{source} connector call failed: {msg}") from exc


def _log(store, user_id: str, connector: str, tool: str, ok: bool, ms: int,
        error: str) -> None:
    try:
        store.log_connector_call(user_id, connector, tool, ok, ms, error)
    except Exception:
        pass   # the ledger is best-effort observability; never break the caller
