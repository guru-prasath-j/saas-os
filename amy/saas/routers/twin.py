"""Digital twin routes: snapshot, ask, full twin engine, personality, future-self."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..db import User
from ..deps import current_user, Query, _engine_for, _user_key, _collab_db_path

router = APIRouter()


class FutureSelfBody(BaseModel):
    title: str
    category: str = "personal"
    reason: str = ""


@router.get("/api/twin")
def twin_snapshot(user: User = Depends(current_user)):
    from ...twin import DigitalTwin
    from ...collab import CollabDB
    eng = _engine_for(user)
    db = CollabDB(_collab_db_path(user))
    try:
        return DigitalTwin(eng.notes, db).snapshot()
    finally:
        db.close()


@router.post("/api/twin/ask")
def twin_ask(q: Query, user: User = Depends(current_user)):
    from ...twin import DigitalTwin
    from ...collab import CollabDB
    from ...llm import LLMRouter
    eng = _engine_for(user)
    db = CollabDB(_collab_db_path(user))
    llm = LLMRouter(openai_api_key=_user_key(user), use_global_keys=False)
    try:
        return DigitalTwin(eng.notes, db, llm=llm).ask(q.text)
    finally:
        db.close()


@router.get("/api/twin/full")
def twin_full(user: User = Depends(current_user)):
    from ...digital_twin import DigitalTwinEngine
    from ...collab import CollabDB
    eng = _engine_for(user)
    db = CollabDB(_collab_db_path(user))
    try:
        return DigitalTwinEngine(eng.notes, db).snapshot()
    finally:
        db.close()


@router.post("/api/twin/full/ask")
def twin_full_ask(q: Query, user: User = Depends(current_user)):
    from ...digital_twin import DigitalTwinEngine
    from ...collab import CollabDB
    eng = _engine_for(user)
    llm = eng.master.classifier.llm
    db = CollabDB(_collab_db_path(user))
    try:
        return DigitalTwinEngine(eng.notes, db, llm=llm).ask(q.text)
    finally:
        db.close()


@router.get("/api/personality")
def personality(user: User = Depends(current_user)):
    from ...digital_twin import PersonalityEngine
    from ...collab import CollabDB
    eng = _engine_for(user)
    db = CollabDB(_collab_db_path(user))
    try:
        return PersonalityEngine(eng.notes, db).profile()
    finally:
        db.close()


@router.post("/api/future-self/validate")
def future_self_validate(body: FutureSelfBody, user: User = Depends(current_user)):
    from ...digital_twin import FutureSelfAgent, PersonalityEngine
    from ...collab import CollabDB
    eng = _engine_for(user)
    db = CollabDB(_collab_db_path(user))
    try:
        prios = PersonalityEngine(eng.notes, db).priorities()
        return FutureSelfAgent(db, priorities=prios).validate(
            body.title, category=body.category, reason=body.reason)
    finally:
        db.close()
