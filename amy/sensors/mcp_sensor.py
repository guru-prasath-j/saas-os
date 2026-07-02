"""Layer 2 — sensor promotion loop for MCP connectors.

Layer 1 (amy/connectors/mcp.py, amy/saas/routers/mcp_connectors.py) makes a
source *queryable*. This module is the separate, explicit step that makes a
*promoted* source (McpConnector.promoted_to_sensor == True) write events —
normalizing its activity and calling events.emit(), the same shape
amy/sensors/github_sensor.py already uses. A connector can be connected and
queryable without ever appearing here.

Phase 1 only wires up GitHub (github_sensor.py already has the normalization
built) — Plane and KITE are Layer-1-only (connect/query) until they're
explicitly promoted with their own normalizer in a later phase.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session


def poll_promoted(db: Session, event_store, user_id: str,
                   github_repos: list[str] | None = None) -> dict[str, int]:
    """Poll every promoted connector owned by user_id. Returns {connector_name: events_published}.

    github_repos: 'owner/repo' list to poll for any promoted GitHub connector
    (there's no per-repo config in the registry yet — caller supplies it,
    e.g. from a future UI field or the digest scheduler).
    """
    from ..saas.db import McpConnector

    rows = db.scalars(
        select(McpConnector).where(
            McpConnector.user_id == user_id,
            McpConnector.promoted_to_sensor == True,  # noqa: E712 (SQLAlchemy needs ==, not is)
        )
    ).all()

    results: dict[str, int] = {}
    for row in rows:
        n = poll_one(row, event_store, github_repos=github_repos)
        if n is not None:
            results[row.name] = n
    return results


def poll_one(row, event_store, github_repos: list[str] | None = None) -> int | None:
    """Dispatch to a per-source normalizer. Returns None if this source has no
    normalizer yet (Layer 1-only — connected but not writing events)."""
    key = f"{row.name} {row.server_url}".lower()
    if "github" in key:
        return _poll_github(row, event_store, github_repos or [])
    return None


def _poll_github(row, event_store, owner_repos: list[str]) -> int:
    from .github_sensor import GitHubSensor
    from .github_service import GitHubService
    from ..saas import security

    # Sourced from this user's registered connector, not the global
    # GITHUB_TOKEN env var — that's what "promoting GitHub to a sensor" means
    # at the per-user SaaS layer.
    token = security.decrypt_secret(row.auth_ref) if row.auth_ref else ""
    service = GitHubService(token=token)
    sensor = GitHubSensor(event_store, service=service)
    if not sensor.authenticated:
        return 0
    total = 0
    for owner_repo in owner_repos:
        total += len(sensor.poll(owner_repo))
    return total
