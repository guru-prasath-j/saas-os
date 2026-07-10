"""Life Autopilot routes (/api/life) — LIFE AUTOPILOT L2 (metrics) + L4
(habit links).

GET /api/life/metrics is read-only over the life_metrics table computed by
the life_metrics_daily job / backfill — no side effects, so L2 is
independently testable via the API ahead of L7's UI landing.

habit_links CRUD is the mechanism a future Add-habit UI (L7) needs to call
to actually create a link — building it now rather than gating it behind
L7 so L4 is usable/testable end-to-end, not just backend logic nobody can
reach.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..db import User
from ..deps import current_user
from .automation import _ctx

router = APIRouter()


class HabitLinkBody(BaseModel):
    signal_type: str
    signal_params: dict = {}
    mode: str = "auto_suggest_check"


_SIGNAL_TYPES = {"geo_place_visit", "txn_absence", "txn_presence", "reading_minutes",
                 "left_office_before", "sleep_window_met", "capture_meal"}
_MODES = {"auto_complete", "auto_suggest_check"}


@router.get("/api/life/metrics")
def list_life_metrics(from_: str = Query("", alias="from"), to: str = "",
                      user: User = Depends(current_user)):
    import datetime as _dt

    since = from_ or (_dt.date.today() - _dt.timedelta(days=30)).isoformat()
    until = to or _dt.date.today().isoformat()
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        rows = ctx.store.list_life_metrics(user.id, since, until)
        return {"metrics": rows, "from": since, "to": until}
    finally:
        cdb.close()


@router.get("/api/life/habits/link-suggestions")
def link_suggestions(title: str, user: User = Depends(current_user)):
    from ...life.habits import suggest_link_for_title
    return {"suggestion": suggest_link_for_title(title)}


@router.post("/api/life/habits/{habit_id}/link")
def add_habit_link(habit_id: str, body: HabitLinkBody, user: User = Depends(current_user)):
    if body.signal_type not in _SIGNAL_TYPES:
        raise HTTPException(status_code=400, detail=f"signal_type must be one of {sorted(_SIGNAL_TYPES)}")
    if body.mode not in _MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {sorted(_MODES)}")
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        habits = ctx.open_habits()
        try:
            exists = habits.db.execute(
                "SELECT 1 FROM habits WHERE id=?", (habit_id,)).fetchone()
        finally:
            habits.close()
        if not exists:
            raise HTTPException(status_code=404, detail="habit not found")
        lid = ctx.store.add_habit_link(user.id, habit_id, body.signal_type,
                                       body.signal_params, body.mode)
        return {"id": lid}
    finally:
        cdb.close()


@router.get("/api/life/habits/{habit_id}/links")
def list_habit_links(habit_id: str, user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        return {"links": ctx.store.list_habit_links(user.id, habit_id)}
    finally:
        cdb.close()


@router.delete("/api/life/habit-links/{link_id}")
def delete_habit_link(link_id: str, user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        link = ctx.store.get_habit_link(link_id)
        if not link or link["uid"] != user.id:
            raise HTTPException(status_code=404, detail="link not found")
        ctx.store.delete_habit_link(link_id)
        return {"ok": True}
    finally:
        cdb.close()
