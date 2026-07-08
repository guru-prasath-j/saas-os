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


class FocusBody(BaseModel):
    focus: str


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
        q += " ORDER BY relevance DESC, fetched_at DESC, score DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        rows = [dict(r) for r in cdb.conn.execute(q, args).fetchall()]
        return {"items": rows, "focus": resolve_focus(cdb.conn)}
    finally:
        cdb.close()


@router.patch("/api/learning-feed/focus")
def set_focus(body: FocusBody, background: BackgroundTasks,
              user: User = Depends(current_user)):
    from ...learning_feed.sensor import FOCUS_PREF_KEY, refresh_for_user
    focus = body.focus.strip()
    if not focus:
        raise HTTPException(status_code=400, detail="focus must not be empty")
    cdb = _open_collab(user)
    try:
        cdb.conn.execute(
            "INSERT INTO prefs(key,value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (FOCUS_PREF_KEY, focus[:200]))
        cdb.conn.commit()
    finally:
        cdb.close()
    # fire-and-forget: BackgroundTasks runs sync functions in a threadpool,
    # so the sensor's internal asyncio.run() is safe there
    background.add_task(refresh_for_user, user.id, focus[:200])
    return {"focus": focus[:200], "refresh": "scheduled"}


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
                from ...events.store import EventStore, LEARNING_ITEM_COMPLETED
                EventStore(cdb).emit(LEARNING_ITEM_COMPLETED, {
                    "title": item["title"], "url": item["url"],
                    "source": item["source"], "focus": item.get("focus_tag"),
                }, source="learning_feed")
            except Exception:
                pass
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
