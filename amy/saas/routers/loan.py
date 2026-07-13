"""Loan Underwriting Module (Phase 5) routes — plain JSON endpoints, no
UI. Illustrative underwriting simulation only — see
amy/finance/loan_engine.py's module docstring. Applying always parks a
tier-2 approval in the normal Approval Inbox (GET/POST
/api/automation/approvals...), same pipeline as every prior phase — there
is no separate loan-specific approve/reject endpoint (see loan_engine.
_reconcile()'s docstring for why rejection is handled by lazy
reconciliation instead).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import User
from ..deps import current_user
from .automation import _ctx
from .finance import _finance_db

router = APIRouter()


class LoanApplyBody(BaseModel):
    loan_type: str
    jurisdiction: str
    amount_requested: float
    term_months: int
    financing_structure: str | None = None


class LoanSimulateBody(LoanApplyBody):
    pass


@router.post("/api/loans/simulate")
def simulate_loan(body: LoanSimulateBody, user: User = Depends(current_user)):
    from ...finance.loan_engine import underwrite

    cdb, ctx = _ctx(user, with_llm=False)
    try:
        fe = ctx.open_finance()
        try:
            try:
                return underwrite(fe, ctx.collab, body.loan_type, body.jurisdiction,
                                  body.amount_requested, body.term_months,
                                  body.financing_structure)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        finally:
            fe.close()
    finally:
        cdb.close()


@router.post("/api/loans/apply")
def apply_for_loan(body: LoanApplyBody, user: User = Depends(current_user)):
    from ...finance.loan_engine import apply_for_loan as _apply

    cdb, ctx = _ctx(user, with_llm=False)
    try:
        try:
            return _apply(ctx, body.loan_type, body.jurisdiction, body.amount_requested,
                         body.term_months, body.financing_structure)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        cdb.close()


@router.get("/api/loans")
def list_loans(status: str | None = None, jurisdiction: str | None = None,
              limit: int = 100, user: User = Depends(current_user)):
    from ...finance.loan_engine import list_applications

    cdb, ctx = _ctx(user, with_llm=False)
    try:
        fe = ctx.open_finance()
        try:
            return {"applications": list_applications(
                fe, ctx.store, status=status, jurisdiction=jurisdiction, limit=limit)}
        finally:
            fe.close()
    finally:
        cdb.close()


@router.get("/api/loans/{application_id}")
def get_loan(application_id: str, user: User = Depends(current_user)):
    from ...finance.loan_engine import get_application

    cdb, ctx = _ctx(user, with_llm=False)
    try:
        fe = ctx.open_finance()
        try:
            app = get_application(fe, ctx.store, application_id)
        finally:
            fe.close()
    finally:
        cdb.close()
    if app is None:
        raise HTTPException(status_code=404, detail="loan application not found")
    return app


@router.get("/api/loans/{application_id}/schedule")
def get_loan_schedule(application_id: str, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        if fe.get_loan_application(application_id) is None:
            raise HTTPException(status_code=404, detail="loan application not found")
        return {"schedule": fe.get_loan_schedule(application_id)}
    finally:
        fe.close()
