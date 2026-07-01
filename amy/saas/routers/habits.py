"""Habits, spaced-repetition (SRS), and entity-extraction routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..db import User
from .. import paths
from ..deps import current_user, _engine_for

router = APIRouter()


# --- habits ------------------------------------------------------------------

class HabitBody(BaseModel):
    title: str
    frequency: str = "daily"
    color: str = "#22D3EE"


class CheckInBody(BaseModel):
    date: str | None = None
    done: bool = True
    note: str = ""


def _habits_db(user: "User"):
    from ...habits import HabitEngine
    return HabitEngine(paths.index_dir(user.id) / "habits.db")


@router.get("/api/habits")
def get_habits(user: User = Depends(current_user)):
    eng = _habits_db(user)
    try:
        return {"habits": eng.list_habits()}
    finally:
        eng.close()


@router.post("/api/habits")
def add_habit(body: HabitBody, user: User = Depends(current_user)):
    eng = _habits_db(user)
    try:
        return {"id": eng.add(body.title, body.frequency, body.color)}
    finally:
        eng.close()


@router.post("/api/habits/{habit_id}/checkin")
def habit_checkin(habit_id: str, body: CheckInBody,
                  user: User = Depends(current_user)):
    eng = _habits_db(user)
    try:
        return eng.check_in(habit_id, body.date, body.done, body.note)
    finally:
        eng.close()


@router.delete("/api/habits/{habit_id}")
def archive_habit(habit_id: str, user: User = Depends(current_user)):
    eng = _habits_db(user)
    try:
        eng.archive(habit_id)
        return {"ok": True}
    finally:
        eng.close()


@router.get("/api/habits/{habit_id}/heatmap")
def habit_heatmap(habit_id: str, days: int = 90, user: User = Depends(current_user)):
    eng = _habits_db(user)
    try:
        return {"heatmap": eng.heatmap(habit_id, days)}
    finally:
        eng.close()


# --- spaced repetition -------------------------------------------------------

class ReviewBody(BaseModel):
    card_id: str
    quality: int  # 0-5


def _srs_db(user: "User"):
    from ...srs import SRSEngine
    return SRSEngine(paths.index_dir(user.id) / "srs.db")


@router.post("/api/srs/build")
def srs_build(user: User = Depends(current_user)):
    eng = _engine_for(user)
    srs = _srs_db(user)
    try:
        return srs.build_from_notes(eng.notes)
    finally:
        srs.close()


@router.get("/api/srs/due")
def srs_due(limit: int = 20, user: User = Depends(current_user)):
    srs = _srs_db(user)
    try:
        return {"cards": srs.due_cards(limit), "stats": srs.stats()}
    finally:
        srs.close()


@router.post("/api/srs/review")
def srs_review(body: ReviewBody, user: User = Depends(current_user)):
    srs = _srs_db(user)
    try:
        return srs.review(body.card_id, body.quality)
    finally:
        srs.close()


@router.get("/api/srs/stats")
def srs_stats(user: User = Depends(current_user)):
    srs = _srs_db(user)
    try:
        return srs.stats()
    finally:
        srs.close()


# --- entity extraction -------------------------------------------------------

def _entities_db(user: "User"):
    from ...entities import EntityExtractor
    return EntityExtractor(paths.index_dir(user.id) / "entities.db")


@router.post("/api/entities/build")
def entities_build(user: User = Depends(current_user)):
    eng = _engine_for(user)
    ext = _entities_db(user)
    try:
        return ext.build(eng.notes)
    finally:
        ext.close()


@router.get("/api/entities")
def list_entities(type: str | None = None, limit: int = 100,
                  min_mentions: int = 2, user: User = Depends(current_user)):
    ext = _entities_db(user)
    try:
        return {"entities": ext.list_entities(type=type, limit=limit,
                                              min_mentions=min_mentions)}
    finally:
        ext.close()


@router.get("/api/entities/search")
def search_entities(q: str, user: User = Depends(current_user)):
    ext = _entities_db(user)
    try:
        return {"results": ext.search(q)}
    finally:
        ext.close()
