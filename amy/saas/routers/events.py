"""Event store and GitHub sensor routes.

The EventStore(db) calls below are intentionally bare (not built via
amy.events.factory.get_events()): amy.sensors.GitHubSensor publishes
"github.NEW_*"/"github.CI_FAILURE" types (see amy/sensors/github_models.py)
that no reactive agent subscribes to today — this is the legacy Operational
Layer github integration (env-token based), separate from the CONNECTOR
COMPLETION phase's MCP-based github.pr_review_requested/etc. sensor
(amy/agents/reactive.py's pr_to_task agent subscribes to THOSE, not these).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..db import User
from ..deps import current_user, _collab_db_path, _journal_user

router = APIRouter()


@router.get("/api/events")
def events_recent(type: str | None = None, limit: int = 50,
                  user: User = Depends(current_user)):
    from ...events import EventStore
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        return {"events": EventStore(db).recent(event_type=type, n=limit)}
    finally:
        db.close()


@router.get("/api/events/stats")
def events_stats(user: User = Depends(current_user)):
    from ...events import EventStore
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        return {"counts": EventStore(db).stats()}
    finally:
        db.close()


@router.post("/api/sensors/github/webhook")
async def github_webhook(request: Request, user: User = Depends(current_user)):
    from ...sensors import GitHubSensor
    from ...events import EventStore
    from ...collab import CollabDB
    event_name = request.headers.get("X-GitHub-Event", "")
    payload = await request.json()
    db = CollabDB(_collab_db_path(user))
    try:
        ev = GitHubSensor(EventStore(db)).ingest_webhook(event_name, payload)
        result = {"ok": True, "published": ev.type if ev else None}
    finally:
        db.close()
    _journal_user(user)
    return result


@router.post("/api/sensors/github/poll")
def github_poll(repo: str, user: User = Depends(current_user)):
    from ...sensors import GitHubSensor
    from ...events import EventStore
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        sensor = GitHubSensor(EventStore(db))
        if not sensor.authenticated:
            return {"ok": False, "detail": "GITHUB_TOKEN not configured"}
        out = sensor.poll(repo)
        result = {"ok": True, "published": [e.type for e in out]}
    finally:
        db.close()
    _journal_user(user)
    return result
