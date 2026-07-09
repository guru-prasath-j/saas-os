"""Learning feed routes — items aggregated from promoted MCP learning-feed
connectors (amy/learning_feed/). Items live in learning_feed_items in the
user's collab.db (table lazily created by AutomationStore._init).
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from ..db import User
from ..deps import current_user, _collab_db_path
from .. import tenancy

router = APIRouter()


class FocusCreateBody(BaseModel):
    topic: str
    goal_id: str | None = None


class FocusUpdateBody(BaseModel):
    active: bool | None = None
    goal_id: str | None = None
    clear_goal: bool = False


class ProgressBody(BaseModel):
    position_sec: float
    duration_sec: float | None = None


_COMPLETE_AT = 0.9   # ≥90% watched counts as completed


def _open_collab(user: User):
    from ...collab import CollabDB
    from ...automation.store import AutomationStore
    cdb = CollabDB(_collab_db_path(user))
    AutomationStore(cdb)   # lazy table creation (learning_feed_items et al.)
    return cdb


@router.get("/api/learning-feed")
def list_feed(source: str | None = None, saved: int | None = None,
              focus_id: str | None = None,
              limit: int = 100, user: User = Depends(current_user)):
    from ...learning_feed.sensor import resolve_focus
    cdb = _open_collab(user)
    try:
        q = "SELECT * FROM learning_feed_items WHERE uid=?"
        args: list = [user.id]
        if source:
            q += " AND source=?"
            args.append(source.strip().lower())
        if saved is not None:
            q += " AND saved=?"
            args.append(1 if saved else 0)
        if focus_id:
            q += " AND focus_id=?"
            args.append(focus_id)
        q += " ORDER BY relevance DESC, fetched_at DESC, score DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        rows = [dict(r) for r in cdb.conn.execute(q, args).fetchall()]
        return {"items": rows, "focus": resolve_focus(cdb.conn)}
    finally:
        cdb.close()


@router.get("/api/learning-feed/focuses")
def list_focuses_route(user: User = Depends(current_user)):
    """All focuses (active + inactive) with their linked goal's title, if
    any. Auto-seeds a first focus from the legacy single-focus pref for a
    user who hasn't created one yet."""
    from ...learning_feed.sensor import list_focuses
    cdb = _open_collab(user)
    try:
        list_focuses(cdb.conn, user.id)   # side effect: seeds default if empty
        rows = cdb.conn.execute(
            "SELECT f.*, g.title AS goal_title FROM learning_focuses f"
            " LEFT JOIN goals g ON f.goal_id = g.id"
            " WHERE f.uid=? ORDER BY f.created_at", (user.id,)).fetchall()
        return {"focuses": [dict(r) for r in rows]}
    finally:
        cdb.close()


@router.post("/api/learning-feed/focuses")
def create_focus(body: FocusCreateBody, background: BackgroundTasks,
                 user: User = Depends(current_user)):
    from ...learning_feed.sensor import add_focus, refresh_for_user
    topic = body.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="topic must not be empty")
    cdb = _open_collab(user)
    try:
        fid = add_focus(cdb.conn, user.id, topic, goal_id=body.goal_id)
    finally:
        cdb.close()
    # fire-and-forget: BackgroundTasks runs sync functions in a threadpool,
    # so the sensor's internal asyncio.run() is safe there. Pass the row id,
    # not the topic text — refreshing by text would silently recreate this
    # focus if the user deletes it before the queued task runs.
    background.add_task(refresh_for_user, user.id, focus_id=fid)
    return {"id": fid, "topic": topic[:200], "refresh": "scheduled"}


@router.patch("/api/learning-feed/focuses/{focus_id}")
def update_focus(focus_id: str, body: FocusUpdateBody, background: BackgroundTasks,
                 user: User = Depends(current_user)):
    from ...learning_feed.sensor import set_focus_active, set_focus_goal, refresh_for_user
    cdb = _open_collab(user)
    try:
        row = cdb.conn.execute(
            "SELECT * FROM learning_focuses WHERE id=? AND uid=?",
            (focus_id, user.id)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="focus not found")
        reactivated = bool(body.active) and not row["active"]
        if body.active is not None:
            set_focus_active(cdb.conn, user.id, focus_id, body.active)
        if body.clear_goal:
            set_focus_goal(cdb.conn, user.id, focus_id, None)
        elif body.goal_id is not None:
            set_focus_goal(cdb.conn, user.id, focus_id, body.goal_id)
    finally:
        cdb.close()
    if reactivated:
        background.add_task(refresh_for_user, user.id, focus_id=focus_id)
    return {"id": focus_id, "updated": True}


@router.delete("/api/learning-feed/focuses/{focus_id}")
def remove_focus(focus_id: str, user: User = Depends(current_user)):
    from ...learning_feed.sensor import delete_focus
    cdb = _open_collab(user)
    try:
        ok = delete_focus(cdb.conn, user.id, focus_id)
    finally:
        cdb.close()
    if not ok:
        raise HTTPException(status_code=404, detail="focus not found")
    return {"deleted": True}


@router.patch("/api/learning-feed/progress/{item_id}")
def track_progress(item_id: str, body: ProgressBody,
                   user: User = Depends(current_user)):
    """Watch-progress heartbeat from the inline player. Stores the resume
    position; the first time progress crosses 90% it writes a 'Watched'
    vault note and emits learning.item_completed (fire-and-forget, same
    stance as _emit_fin in finance.py)."""
    pos = max(0.0, body.position_sec)
    cdb = _open_collab(user)
    try:
        row = cdb.conn.execute(
            "SELECT * FROM learning_feed_items WHERE id=? AND uid=?",
            (item_id, user.id)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="feed item not found")
        item = dict(row)
        duration = body.duration_sec or item.get("duration_sec") or 0
        progress = min(1.0, pos / duration) if duration > 0 else item.get("progress") or 0
        # a heartbeat can't regress completion (rewinding to the start
        # shouldn't un-complete the video)
        progress = max(progress, item.get("progress") or 0)
        just_completed = (progress >= _COMPLETE_AT
                          and not item.get("completed_at"))
        import datetime as _dt
        completed_at = (_dt.datetime.now(_dt.timezone.utc).isoformat()
                        if just_completed else item.get("completed_at"))
        cdb.conn.execute(
            "UPDATE learning_feed_items SET position_sec=?, duration_sec=?,"
            " progress=?, completed_at=? WHERE id=? AND uid=?",
            (int(pos), int(duration) if duration else None, progress,
             completed_at, item_id, user.id))
        cdb.conn.commit()

        if just_completed:
            try:
                from ...events.store import LEARNING_ITEM_COMPLETED
                from ...events.factory import get_events
                from .. import paths
                # amy.events.factory.get_events() (Part 0 / quirk 20 fix) wires
                # reactive agents onto THIS EventStore instance before
                # emitting, so the learning agent reacts
                es = get_events(user.id, cdb, index_dir=paths.index_dir(user.id),
                                user_email=user.email)
                es.emit(LEARNING_ITEM_COMPLETED, {
                    "title": item["title"], "url": item["url"],
                    "source": item["source"], "focus": item.get("focus_tag"),
                    "focus_id": item.get("focus_id"),
                }, source="learning_feed")
            except Exception:
                pass
            try:
                from ...collab.memory import MemoryManager
                MemoryManager(cdb).log_activity(
                    "learning", item["title"], domain=item.get("focus_tag"))
            except Exception:
                pass   # activity log is best-effort; progress is already saved
    finally:
        cdb.close()

    note = None
    if just_completed:
        try:
            from ...memory.writer import MemoryWriter
            vault = tenancy.resolve_vault_dir(user.id)
            if vault.exists():
                body_md = (f"[{item['title']}]({item['url']})\n\n"
                           f"- Source: `{item['source']}`\n"
                           f"- Focus: {item.get('focus_tag') or ''}\n"
                           f"- Completed: yes (≥90% watched)\n"
                           + (f"\n{item['summary']}\n" if item.get("summary") else ""))
                p = MemoryWriter(vault).write_atomic(
                    "watched", (item["title"] or "feed item")[:50], body_md,
                    eid=f"feedwatch-{item_id}", tags=["learning", "watched"])
                note = str(p) if p else "already-written"
        except Exception:
            note = None   # progress is saved; the note is best-effort

    return {"progress": round(progress, 3), "position_sec": int(pos),
            "completed": bool(completed_at), "note": note}


@router.post("/api/learning-feed/save/{item_id}")
def save_item(item_id: str, user: User = Depends(current_user)):
    cdb = _open_collab(user)
    try:
        row = cdb.conn.execute(
            "SELECT * FROM learning_feed_items WHERE id=? AND uid=?",
            (item_id, user.id)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="feed item not found")
        cdb.conn.execute(
            "UPDATE learning_feed_items SET saved=1 WHERE id=? AND uid=?",
            (item_id, user.id))
        cdb.conn.commit()
        item = dict(row)
        try:
            from ...collab.memory import MemoryManager
            MemoryManager(cdb).log_activity(
                "learning", item["title"], domain=item.get("focus_tag"))
        except Exception:
            pass   # activity log is best-effort; the save already succeeded
    finally:
        cdb.close()

    note = None
    try:
        from ...memory.writer import MemoryWriter
        vault = tenancy.resolve_vault_dir(user.id)
        if vault.exists():
            body = (f"[{item['title']}]({item['url']})\n\n"
                    f"- Source: `{item['source']}`\n"
                    + (f"- Why it matters: {item['why']}\n" if item.get("why") else "")
                    + (f"\n{item['summary']}\n" if item.get("summary") else ""))
            p = MemoryWriter(vault).write_atomic(
                "saved", (item["title"] or "feed item")[:50], body,
                eid=f"feedsave-{item_id}", tags=["learning", "saved"])
            note = str(p) if p else "already-written"
    except Exception:
        note = None   # saving the flag already succeeded; the note is best-effort

    return {"saved": True, "id": item_id, "note": note}
