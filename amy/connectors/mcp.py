"""Generic MCP (Model Context Protocol) client — Layer 1 of the connector
architecture (see "saas os mcp connectors prompt v2.md").

Given any MCP-compatible server URL + auth, MCPConnector can list its tools
and call them. It has no knowledge of any specific source (GitHub, Plane,
KITE, ...) — that's the whole point: adding a new same-shape source needs no
code, just a row in the mcp_connectors table (amy/saas/db.py).

Connecting and calling tools does NOT by itself write to the vault or event
log. That's Layer 2 (amy/sensors/mcp_sensor.py) — a separate, explicit step.

Each call opens a fresh session and closes it — consistent with the rest of
this codebase's per-request (not long-lived-daemon) style.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any


def describe_error(exc: BaseException) -> str:
    """Unwrap anyio TaskGroup ExceptionGroups down to the real cause.

    anyio wraps every error from inside a task group (which is how the mcp
    SDK's transports run) in a BaseExceptionGroup, so a plain str(exc) on the
    outer exception is always the unhelpful "unhandled errors in a TaskGroup
    (1 sub-exception)" — never the actual reason. In particular an HTTP
    error response (401/403/404/...) gets buried this way and looks
    indistinguishable from a genuine network failure. Walk into the group to
    find an httpx.HTTPStatusError (or the innermost real exception) instead.
    """
    seen = exc
    while isinstance(seen, BaseExceptionGroup) and seen.exceptions:
        seen = seen.exceptions[0]
    try:
        import httpx
        if isinstance(seen, httpx.HTTPStatusError):
            code = seen.response.status_code
            hint = " — check your token/credentials" if code in (401, 403) else \
                   " — check the server URL" if code == 404 else ""
            return f"HTTP {code} from {seen.request.url}{hint}"
    except ImportError:
        pass
    return str(seen) or repr(seen)


class MCPConnector:
    # Explicit, not left to SDK defaults. Two separate timeouts the SDK
    # tracks independently:
    #  - connect: sse_client defaults to 5s (too short for NAT64/higher-
    #    latency networks), streamablehttp_client defaults to 30s.
    #  - sse_read: how long to wait for the actual response after connecting
    #    — BOTH transports default this to 300s (5 min), which is why a stuck
    #    call silently "runs" for minutes instead of failing fast. 60s is
    #    already generous for a single tool call.
    CONNECT_TIMEOUT = 30.0
    READ_TIMEOUT = 60.0

    def __init__(self, server_url: str, auth_type: str = "none", auth_value: str | None = None,
                 transport: str = "http", auth_extra: str | None = None):
        self.server_url = server_url
        self.auth_type = auth_type
        self.auth_value = auth_value
        self.transport = transport  # "http" (streamable-HTTP, current spec default) | "sse" (legacy)
        # A second, non-secret auth parameter some servers need alongside the
        # token (e.g. Plane's workspace slug — see _headers()).
        self.auth_extra = auth_extra

    def _headers(self) -> dict[str, str]:
        if self.auth_type not in ("api_key", "oauth") or not self.auth_value:
            return {}
        if self.auth_extra:
            # Plane's MCP server (mcp.plane.so/http/api-key/mcp) needs the
            # token as a normal Bearer header *plus* the workspace slug as a
            # separate header — confirmed directly against the live server;
            # x-api-key/x-workspace-slug (what this used to send) gets a
            # generic 401 "invalid_token" regardless of credential validity.
            # auth_extra is currently only used for this shape; if a future
            # server needs a *different* extra-header scheme, this needs to
            # branch on something more specific than "auth_extra is set"
            # (e.g. a per-connector header-scheme field).
            return {"Authorization": f"Bearer {self.auth_value}", "X-Workspace-slug": self.auth_extra}
        return {"Authorization": f"Bearer {self.auth_value}"}

    @asynccontextmanager
    async def _session(self):
        from mcp import ClientSession

        if self.transport == "sse":
            from mcp.client.sse import sse_client
            async with sse_client(self.server_url, headers=self._headers(),
                                   timeout=self.CONNECT_TIMEOUT,
                                   sse_read_timeout=self.READ_TIMEOUT) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        else:
            from mcp.client.streamable_http import streamablehttp_client
            async with streamablehttp_client(self.server_url, headers=self._headers(),
                                              timeout=self.CONNECT_TIMEOUT,
                                              sse_read_timeout=self.READ_TIMEOUT) as (read, write, _get_sid):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    async def list_tools(self) -> list[dict[str, Any]]:
        async with self._session() as session:
            result = await session.list_tools()
            return [
                {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
                for t in result.tools
            ]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._session() as session:
            result = await session.call_tool(name, arguments or {})
            parts = []
            for block in result.content:
                btype = getattr(block, "type", None)
                if btype == "text":
                    parts.append(block.text)
                elif btype == "resource":
                    # File contents (e.g. GitHub's get_file_contents) come back as an
                    # EmbeddedResource, not a plain text block — the real content is
                    # nested at block.resource.text (or .blob for binary files), never
                    # at block.text directly. Missing this silently drops all file reads.
                    res = block.resource
                    if hasattr(res, "text"):
                        parts.append(res.text)
                    else:
                        mime = getattr(res, "mimeType", None) or "unknown type"
                        parts.append(f"[binary resource, {mime}: {res.uri}]")
                elif btype == "resource_link":
                    parts.append(f"[resource link: {block.name} — {block.uri}]")
            return {
                "is_error": result.isError,
                "text": "\n".join(parts),
                "structured": result.structuredContent,
            }


def call_tool_sync(connector: "MCPConnector", name: str, arguments: dict[str, Any] | None = None,
                    timeout: float = 15.0) -> dict[str, Any] | None:
    """Bridge for calling call_tool() from synchronous code that may already
    be running inside an event loop (e.g. a FastAPI request handler calling
    into sync agent/orchestrator code) — a plain asyncio.run() would raise
    "cannot be called from a running event loop" there. Runs the call in a
    fresh thread with its own event loop instead.

    Returns None on any failure or timeout rather than raising — this is
    meant for best-effort context enrichment (e.g. injecting live Plane data
    into a chat answer) that must never break the caller.
    """
    import threading

    result: dict[str, Any] | None = None
    error: BaseException | None = None

    def _runner():
        nonlocal result, error
        import asyncio
        try:
            result = asyncio.run(connector.call_tool(name, arguments))
        except BaseException as e:
            error = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout)
    if error is not None or result is None:
        return None
    return result
