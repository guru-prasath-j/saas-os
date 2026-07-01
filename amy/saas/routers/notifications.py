"""Notification endpoints — in-app alert feed + SSE stream."""
from __future__ import annotations

import json as _json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from ..db import User
from ..deps import current_user, _collab_db_path

router = APIRouter()


def _store(user: User):
    from ...notifications import NotificationStore
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    return db, NotificationStore(db)


# ---------------------------------------------------------------------------
# REST polling endpoints
# ---------------------------------------------------------------------------

@router.get("/api/notifications")
def list_notifications(unread_only: bool = False, limit: int = 50,
                       user: User = Depends(current_user)):
    """Return recent notifications, newest first."""
    db, store = _store(user)
    try:
        notifications = store.list(unread_only=unread_only, limit=limit)
        return {
            "notifications": notifications,
            "unread_count": store.unread_count(),
        }
    finally:
        db.close()


@router.post("/api/notifications/{nid}/read")
def mark_read(nid: str, user: User = Depends(current_user)):
    """Mark a single notification as read."""
    db, store = _store(user)
    try:
        if not store.mark_read(nid):
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="notification not found")
        return {"ok": True}
    finally:
        db.close()


@router.post("/api/notifications/read-all")
def mark_all_read(user: User = Depends(current_user)):
    """Mark all unread notifications as read."""
    db, store = _store(user)
    try:
        store.mark_all_read()
        return {"ok": True}
    finally:
        db.close()


@router.get("/api/notifications/count")
def unread_count(user: User = Depends(current_user)):
    db, store = _store(user)
    try:
        return {"unread_count": store.unread_count()}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# SSE stream (optional; SPA can poll instead)
# Emits current unread notifications immediately, then a heartbeat every 30s.
# ---------------------------------------------------------------------------

@router.get("/api/notifications/stream")
def notification_stream(user: User = Depends(current_user)):
    """
    Server-Sent Events stream for real-time notification delivery.

    On connect: emits all unread notifications as individual 'notification' events.
    Periodically: emits a 'heartbeat' event with unread count.
    SPA can disconnect after reading the initial batch and fall back to polling.
    """
    import asyncio
    import time

    def _sse(event: str, data) -> str:
        return f"event: {event}\ndata: {_json.dumps(data)}\n\n"

    # Capture user id and db path before entering the generator
    collab_path = _collab_db_path(user)
    uid = user.id

    async def gen():
        from ...notifications import NotificationStore
        from ...collab import CollabDB
        # Emit all currently unread notifications
        db = CollabDB(collab_path)
        try:
            store = NotificationStore(db)
            for n in store.list(unread_only=True):
                yield _sse("notification", n)
            yield _sse("heartbeat", {"unread_count": store.unread_count()})
        finally:
            db.close()

        # Keep-alive: resend heartbeat every 30 s
        while True:
            await asyncio.sleep(30)
            db2 = CollabDB(collab_path)
            try:
                store2 = NotificationStore(db2)
                yield _sse("heartbeat", {"unread_count": store2.unread_count()})
            except Exception:
                break
            finally:
                db2.close()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
