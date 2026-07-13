"""Amy Credit Score Module (Phase 3) routes — plain JSON endpoints, no UI.

Illustrative internal score only — "Amy Score, not a credit bureau score."
See amy/finance/credit_engine.py's module docstring. No approval gating
anywhere here — computing/storing the score is a read-model refresh, not
an external or money-moving action (contrast with the Fraud/AML routers).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..db import User
from ..deps import current_user
from .automation import _ctx
from .finance import _finance_db

router = APIRouter()


@router.post("/api/credit/recompute")
def recompute_credit_score(user: User = Depends(current_user)):
    from ...finance.credit_engine import record_score

    cdb, ctx = _ctx(user, with_llm=False)
    try:
        return record_score(ctx)
    finally:
        cdb.close()


@router.get("/api/credit/score")
def get_credit_score(user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        latest = fe.get_latest_credit_score()
    finally:
        fe.close()
    if latest is None:
        return {"available": False, "reason": "no Amy Score has been computed yet — "
                                              "POST /api/credit/recompute first"}
    return {"available": True, **latest}


@router.get("/api/credit/history")
def get_credit_score_history(limit: int = 100, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        return {"history": fe.list_credit_score_history(limit=limit)}
    finally:
        fe.close()
