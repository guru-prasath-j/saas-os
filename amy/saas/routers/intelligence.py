"""Intelligence layer: decisions, predictions, simulate, context, goals (autonomous),
executive, autopilot, timeline, universal search, and unified recall."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import User
from ..deps import (
    current_user, Query,
    _engine_for, _user_key, _collab_db_path, _connector_dir,
)

router = APIRouter()


# --- schemas -----------------------------------------------------------------

class TaskBody(BaseModel):
    title: str


class DepBody(BaseModel):
    depends_on: str


class DecisionBody(BaseModel):
    title: str
    reason: str = ""
    category: str = "personal"
    confidence: float | None = None


class OutcomeBody(BaseModel):
    outcome: str
    status: str = "resolved"


class DecisionV2Body(BaseModel):
    title: str
    category: str = "personal"
    reason: str = ""
    confidence: float | None = None


class SimBody(BaseModel):
    scenario: str
    params: dict = {}


class SearchBody(BaseModel):
    query: str
    sources: list[str] | None = None
    limit: int = 10
    offset: int = 0


class ModeBody(BaseModel):
    mode: str


# --- helpers -----------------------------------------------------------------

def _decision_engine(user: "User"):
    from ...engines import DecisionEngine
    from ...events import EventStore
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    return db, DecisionEngine(db, events=EventStore(db))


def _timeline_grouped(period: str, user):
    from ...intelligence import TimelineEngine
    from ...collab import CollabDB
    eng = _engine_for(user)
    db = CollabDB(_collab_db_path(user))
    try:
        return {"period": period,
                "groups": TimelineEngine(db).grouped(
                    period, notes=eng.notes, connector_dir=_connector_dir(user))}
    finally:
        db.close()


# --- decisions ---------------------------------------------------------------

@router.post("/api/decisions")
def record_decision(body: DecisionBody, user: User = Depends(current_user)):
    db, eng = _decision_engine(user)
    try:
        return {"id": eng.record(body.title, category=body.category,
                                 reason=body.reason, confidence=body.confidence)}
    finally:
        db.close()


@router.post("/api/decisions/{decision_id}/outcome")
def decision_outcome(decision_id: str, body: OutcomeBody,
                     user: User = Depends(current_user)):
    db, eng = _decision_engine(user)
    try:
        eng.set_outcome(decision_id, body.outcome, body.status)
        return {"ok": True}
    finally:
        db.close()


@router.get("/api/decisions")
def list_decisions(limit: int = 100, user: User = Depends(current_user)):
    db, eng = _decision_engine(user)
    try:
        return {"decisions": eng.history(limit=limit)}
    finally:
        db.close()


@router.post("/api/decisions/v2")
def record_decision_v2(body: DecisionV2Body, user: User = Depends(current_user)):
    from ...engines import DecisionEngine
    from ...events import EventStore
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        return {"id": DecisionEngine(db, events=EventStore(db)).record(
            body.title, category=body.category, reason=body.reason,
            confidence=body.confidence)}
    finally:
        db.close()


@router.get("/api/decisions/history")
def decisions_history(category: str | None = None, limit: int = 200,
                      user: User = Depends(current_user)):
    from ...engines import DecisionEngine
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        return {"decisions": DecisionEngine(db).history(category=category, limit=limit)}
    finally:
        db.close()


@router.get("/api/decisions/analysis")
def decisions_analysis(user: User = Depends(current_user)):
    from ...engines import DecisionEngine
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        return DecisionEngine(db).analyze()
    finally:
        db.close()


@router.get("/api/decisions/recommendations")
def decisions_recommendations(category: str | None = None,
                               user: User = Depends(current_user)):
    from ...engines import DecisionEngine
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        return {"recommendations": DecisionEngine(db).recommend(category)}
    finally:
        db.close()


# --- predictive engine -------------------------------------------------------

@router.get("/api/predict/goals")
def predict_goals(user: User = Depends(current_user)):
    from ...engines import PredictiveEngine
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        return {"forecasts": PredictiveEngine(db).forecast_goals()}
    finally:
        db.close()


@router.get("/api/predict/{metric}")
def predict_metric(metric: str, user: User = Depends(current_user)):
    from ...engines import PredictiveEngine
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        p = PredictiveEngine(db)
        fn = {"learning": p.forecast_learning, "career": p.forecast_career,
              "productivity": p.forecast_productivity}.get(metric)
        if fn is None:
            raise HTTPException(status_code=404, detail="unknown metric")
        return fn()
    finally:
        db.close()


# --- simulation engine -------------------------------------------------------

@router.post("/api/simulate")
def simulate(body: SimBody, user: User = Depends(current_user)):
    from ...engines import SimulationEngine
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        return SimulationEngine(db).simulate(body.scenario, **(body.params or {}))
    finally:
        db.close()


# --- autonomous goals / executive -------------------------------------------

@router.get("/api/goals/overview")
def goals_overview(user: User = Depends(current_user)):
    from ...autonomous import GoalEngine
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        return {"goals": GoalEngine(db).overview()}
    finally:
        db.close()


@router.post("/api/goals/{goal_id}/tasks")
def add_task(goal_id: str, body: TaskBody, user: User = Depends(current_user)):
    from ...autonomous import GoalEngine
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        return {"id": GoalEngine(db).add_task(goal_id, body.title)}
    finally:
        db.close()


@router.post("/api/tasks/{task_id}/complete")
def complete_task(task_id: str, done: bool = True, user: User = Depends(current_user)):
    from ...autonomous import GoalEngine
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        GoalEngine(db).complete_task(task_id, done)
        return {"ok": True}
    finally:
        db.close()


@router.post("/api/goals/{goal_id}/depends")
def add_dependency(goal_id: str, body: DepBody, user: User = Depends(current_user)):
    from ...autonomous import GoalEngine
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        GoalEngine(db).add_dependency(goal_id, body.depends_on)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        db.close()


@router.get("/api/executive")
def executive_brief(user: User = Depends(current_user)):
    from ...autonomous import ExecutiveAgent
    from ...events import EventStore
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        return ExecutiveAgent(db).brief(events=EventStore(db))
    finally:
        db.close()


# --- autopilot ---------------------------------------------------------------

@router.post("/api/autopilot/run")
def autopilot_run(dry_run: bool = False, user: User = Depends(current_user)):
    from ...autonomous import Autopilot
    from ...events import EventStore
    from ...llm import LLMRouter
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        ap = Autopilot(db,
                       llm=LLMRouter(openai_api_key=_user_key(user), use_global_keys=False),
                       events=EventStore(db))
        return ap.run(dry_run=dry_run)
    finally:
        db.close()


# --- context engine ----------------------------------------------------------

@router.get("/api/context")
def context_profile(user: User = Depends(current_user)):
    from ...engines import ContextEngine
    from ...events import EventStore
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        return ContextEngine(db).profile(events=EventStore(db))
    finally:
        db.close()


@router.post("/api/context/mode")
def set_context_mode(body: ModeBody, user: User = Depends(current_user)):
    from ...engines import ContextEngine
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        ContextEngine(db).set_mode(body.mode)
        return {"ok": True, "mode": body.mode}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        db.close()


# --- timeline ----------------------------------------------------------------

@router.get("/api/timeline")
def timeline(limit: int = 100, source: str | None = None, q: str | None = None,
             user: User = Depends(current_user)):
    from ...intelligence import TimelineEngine
    from ...collab import CollabDB
    eng = _engine_for(user)
    db = CollabDB(_collab_db_path(user))
    try:
        srcs = [s.strip() for s in source.split(",")] if source else None
        tl = TimelineEngine(db)
        return {"timeline": tl.build(notes=eng.notes, connector_dir=_connector_dir(user),
                                     sources=srcs, query=q, limit=limit),
                "summary": tl.summary(notes=eng.notes,
                                      connector_dir=_connector_dir(user))}
    finally:
        db.close()


@router.get("/api/timeline/day")
def timeline_day(user: User = Depends(current_user)):
    return _timeline_grouped("day", user)


@router.get("/api/timeline/week")
def timeline_week(user: User = Depends(current_user)):
    return _timeline_grouped("week", user)


@router.get("/api/timeline/month")
def timeline_month(user: User = Depends(current_user)):
    return _timeline_grouped("month", user)


# --- universal search --------------------------------------------------------

@router.post("/api/search")
def universal_search(body: SearchBody, user: User = Depends(current_user)):
    from ...search import UniversalSearch
    from ...collab import CollabDB
    eng = _engine_for(user)
    db = CollabDB(_collab_db_path(user))
    try:
        return UniversalSearch(eng.notes, db,
                               connector_dir=_connector_dir(user)).search(
            body.query, sources=body.sources, limit=body.limit, offset=body.offset)
    finally:
        db.close()


# --- unified recall ----------------------------------------------------------

@router.post("/api/recall")
def unified_recall(q: Query, user: User = Depends(current_user)):
    from ...autonomous import UnifiedMemory
    from ...collab import CollabDB
    eng = _engine_for(user)
    db = CollabDB(_collab_db_path(user))
    try:
        return UnifiedMemory(eng.notes, db,
                             connector_dir=_connector_dir(user)).recall(q.text)
    finally:
        db.close()
