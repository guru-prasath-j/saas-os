"""Fraud Detection Module (Phase 1) routes — plain JSON endpoints, no UI.

Illustrative/simulated scoring only — see amy/finance/fraud_engine.py's
module docstring. Reuses the existing approvals/tier pipeline: LOW/MEDIUM
scores apply immediately, HIGH/CRITICAL park in the normal Approval Inbox
(GET/POST /api/automation/approvals...) rather than getting a parallel
review queue here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..db import User
from ..deps import current_user
from .automation import _ctx
from .finance import _finance_db

router = APIRouter()


@router.post("/api/finance/fraud/transactions/{tid}/review")
def review_fraud_transaction(tid: str, user: User = Depends(current_user)):
    """Score a transaction and route it through the tier pipeline. No LLM
    involved — the scorer is pure rule-based (amy/finance/fraud_engine.py)."""
    from ...finance.fraud_engine import review_transaction

    fe = _finance_db(user)
    try:
        if fe.get_transaction(tid) is None:
            raise HTTPException(status_code=404, detail="transaction not found")
    finally:
        fe.close()

    cdb, ctx = _ctx(user, with_llm=False)
    try:
        return review_transaction(ctx, tid)
    finally:
        cdb.close()


@router.get("/api/finance/fraud/transactions/{tid}")
def get_fraud_score(tid: str, user: User = Depends(current_user)):
    """The STORED score only — never re-scores. HIGH/CRITICAL scores show up
    here only once a human has approved the review (see the module
    docstring's tier explanation)."""
    fe = _finance_db(user)
    try:
        if fe.get_transaction(tid) is None:
            raise HTTPException(status_code=404, detail="transaction not found")
        stored = fe.get_fraud_score(tid)
    finally:
        fe.close()
    if stored is None:
        return {"available": False,
               "reason": "not yet reviewed (or its score is a pending approval)"}
    return {"available": True, "transaction_id": tid, **stored}


@router.get("/api/finance/fraud/flagged")
def list_flagged_fraud_transactions(risk_level: str | None = None, limit: int = 100,
                                    user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        return {"transactions": fe.list_flagged_transactions(risk_level=risk_level, limit=limit)}
    finally:
        fe.close()
