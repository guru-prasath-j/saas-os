"""Product surface: profile, portfolio, dashboard, agents, suggestions, cards, digest."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..db import User
from .. import paths
from ..deps import (
    current_user,
    _engine_for, _collab_db_path, _collab_light, _knowledge_for,
)

router = APIRouter()


@router.get("/api/profile")
def profile(user: User = Depends(current_user)):
    from ...product import build_profile
    from ...collab import CollabDB
    eng = _engine_for(user)
    db = CollabDB(_collab_db_path(user))
    try:
        return build_profile(eng.notes, collab_db=db)
    finally:
        db.close()


@router.get("/api/portfolio")
def portfolio(user: User = Depends(current_user)):
    from ...product import build_portfolio
    return build_portfolio(_engine_for(user).notes)


@router.get("/api/dashboard")
def dashboard(user: User = Depends(current_user)):
    from ...product import build_dashboard
    from ...collab import CollabDB
    from ...finance import FinanceEngine
    eng = _engine_for(user)
    db = CollabDB(_collab_db_path(user))
    kb = _knowledge_for(user)
    finance_db_path = paths.index_dir(user.id) / "finance.db"
    fdb = FinanceEngine(str(finance_db_path)) if finance_db_path.exists() else None
    try:
        return build_dashboard(eng.notes, db, knowledge=kb, finance_db=fdb)
    finally:
        db.close()
        kb.close()
        if fdb is not None:
            fdb.close()


@router.get("/api/agents")
def agents_list(user: User = Depends(current_user)):
    from ...product import Marketplace
    from ...collab import CollabDB
    from ...pkos import detect
    eng = _engine_for(user)
    db = CollabDB(_collab_db_path(user))
    try:
        names = [f"{d}_agent" for d in detect(eng.notes)] + ["planner_agent"]
        return {"agents": Marketplace(db).listing(names)}
    finally:
        db.close()


@router.post("/api/agents/{agent}/enable")
def agent_enable(agent: str, user: User = Depends(current_user)):
    from ...product import Marketplace
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        Marketplace(db).enable(agent)
        return {"ok": True, "agent": agent, "enabled": True}
    finally:
        db.close()


@router.post("/api/agents/{agent}/disable")
def agent_disable(agent: str, user: User = Depends(current_user)):
    from ...product import Marketplace
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        Marketplace(db).disable(agent)
        return {"ok": True, "agent": agent, "enabled": False}
    finally:
        db.close()


@router.get("/api/suggestions")
def suggestions(window_days: int = 7, user: User = Depends(current_user)):
    from ...product import build_suggestions
    db, _, planner, reflection, learning = _collab_light(user)
    try:
        return build_suggestions(learning, reflection, planner, window_days)
    finally:
        db.close()


@router.get("/api/cards")
def agent_cards(user: User = Depends(current_user)):
    from ...collab import CollabDB, AgentCards
    db = CollabDB(_collab_db_path(user))
    try:
        return {"cards": AgentCards(db).all()}
    finally:
        db.close()


@router.get("/api/digest")
def digest(days: int = 7, user: User = Depends(current_user)):
    from ...events import build_digest
    from ...product import build_suggestions
    db, _, planner, reflection, learning = _collab_light(user)
    try:
        return build_digest(reflection, learning, planner, build_suggestions, days)
    finally:
        db.close()


@router.get("/api/digest/latest")
def digest_latest(user: User = Depends(current_user)):
    from ...events import EventStore
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        ev = EventStore(db).recent("digest.generated", 1)
        return ev[0] if ev else {"detail": "no digest generated yet"}
    finally:
        db.close()
