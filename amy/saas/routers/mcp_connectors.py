"""Layer 1 — generic MCP connector registration (no per-source code).

Register any MCP-compatible server (name, URL, auth) and its tools become
callable. This does NOT write to the vault or event log — see Layer 2
(amy/sensors/mcp_sensor.py + the `promoted_to_sensor` flag) for that.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import User, McpConnector, get_db
from .. import security
from ..deps import current_user, _collab_db_path, _journal_user

router = APIRouter()

RISK_TIERS = ("official", "platform_api", "scraping_backed", "unofficial_risky")
AUTH_TYPES = ("none", "api_key", "oauth")


class ConnectorCreate(BaseModel):
    name: str
    server_url: str
    auth_type: str = "none"
    auth_value: str | None = None  # plaintext in transit only; encrypted before storage
    risk_tier: str
    auth_extra: str | None = None  # non-secret, e.g. a Plane workspace slug — stored plaintext


class CallToolBody(BaseModel):
    name: str
    arguments: dict | None = None


class PollBody(BaseModel):
    github_repos: list[str] = []


def _get_owned(db: Session, user: User, connector_id: str) -> McpConnector:
    row = db.get(McpConnector, connector_id)
    if not row or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="connector not found")
    return row


def _to_dict(row: McpConnector) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "server_url": row.server_url,
        "auth_type": row.auth_type,
        "auth_extra": row.auth_extra,
        "default_target": row.default_target,
        "risk_tier": row.risk_tier,
        "promoted_to_sensor": row.promoted_to_sensor,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _client_for(row: McpConnector, transport: str = "http"):
    from ...connectors.mcp import MCPConnector
    auth_value = security.decrypt_secret(row.auth_ref) if row.auth_ref else None
    # Legacy-SSE servers (e.g. jobspy-mcp-server) expose an /sse endpoint —
    # the UI has no transport picker, so infer it from the URL when the
    # caller didn't explicitly override.
    if transport == "http" and row.server_url.rstrip("/").endswith("/sse"):
        transport = "sse"
    return MCPConnector(row.server_url, auth_type=row.auth_type, auth_value=auth_value,
                         transport=transport, auth_extra=row.auth_extra)


@router.post("/api/mcp/connectors")
def create_connector(body: ConnectorCreate, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    if body.risk_tier not in RISK_TIERS:
        raise HTTPException(status_code=400, detail=f"risk_tier must be one of {RISK_TIERS}")
    if body.auth_type not in AUTH_TYPES:
        raise HTTPException(status_code=400, detail=f"auth_type must be one of {AUTH_TYPES}")
    if not body.name.strip() or not body.server_url.strip():
        raise HTTPException(status_code=400, detail="name and server_url are required")
    row = McpConnector(
        user_id=user.id,
        name=body.name.strip(),
        server_url=body.server_url.strip(),
        auth_type=body.auth_type,
        auth_ref=security.encrypt_secret(body.auth_value) if body.auth_value else None,
        risk_tier=body.risk_tier,
        promoted_to_sensor=False,
        auth_extra=body.auth_extra.strip() if body.auth_extra else None,
    )
    db.add(row)
    db.commit()
    return _to_dict(row)


@router.get("/api/mcp/connectors")
def list_connectors(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(McpConnector).where(McpConnector.user_id == user.id)).all()
    return {"connectors": [_to_dict(r) for r in rows]}


@router.delete("/api/mcp/connectors/{connector_id}")
def delete_connector(connector_id: str, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    row = _get_owned(db, user, connector_id)
    db.delete(row)
    db.commit()
    return {"deleted": True}


@router.patch("/api/mcp/connectors/{connector_id}/promote")
def promote_connector(connector_id: str, promoted: bool = True,
                       user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Layer 2 toggle — separate and explicit from registering the connector."""
    row = _get_owned(db, user, connector_id)
    row.promoted_to_sensor = promoted
    db.commit()
    return _to_dict(row)


class TargetBody(BaseModel):
    target: str = ""   # e.g. "owner/repo"; empty clears it


@router.patch("/api/mcp/connectors/{connector_id}/target")
def set_connector_target(connector_id: str, body: TargetBody,
                          user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Persist the source's primary target server-side so the background
    poller and agent tools know what to act on (the UI keeps a localStorage
    copy for prefills; this is the copy automation reads)."""
    row = _get_owned(db, user, connector_id)
    row.default_target = body.target.strip()[:300] or None
    db.commit()
    return _to_dict(row)


@router.post("/api/mcp/connectors/{connector_id}/tools")
async def list_tools(connector_id: str, transport: str = "http",
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
    from ...connectors.mcp import describe_error
    row = _get_owned(db, user, connector_id)
    try:
        tools = await _client_for(row, transport).list_tools()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"could not reach MCP server: {describe_error(e)}")
    return {"tools": tools}


@router.post("/api/mcp/connectors/{connector_id}/call")
async def call_tool(connector_id: str, body: CallToolBody, transport: str = "http",
                     user: User = Depends(current_user), db: Session = Depends(get_db)):
    from ...connectors.mcp import describe_error
    row = _get_owned(db, user, connector_id)
    try:
        result = await _client_for(row, transport).call_tool(body.name, body.arguments)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"tool call failed: {describe_error(e)}")
    return result


@router.post("/api/mcp/connectors/{connector_id}/poll")
def poll_connector(connector_id: str, body: PollBody, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    """Manual Layer-2 trigger — lets you verify a real event lands in 00_Daily/
    without waiting for a background scheduler (none exists for MCP sources yet)."""
    row = _get_owned(db, user, connector_id)
    if not row.promoted_to_sensor:
        raise HTTPException(status_code=400,
                             detail="connector is not promoted — enable 'Also sync to vault' first")
    from ...collab import CollabDB
    from ...events.store import EventStore
    from ...sensors.mcp_sensor import poll_one

    cdb = CollabDB(_collab_db_path(user))
    try:
        n = poll_one(row, EventStore(cdb), github_repos=body.github_repos)
    finally:
        cdb.close()
    if n is None:
        raise HTTPException(status_code=400,
                             detail=f"no Layer-2 normalizer for '{row.name}' yet (only GitHub is wired)")
    journal = _journal_user(user)
    return {"events_published": n, "journal": journal}
