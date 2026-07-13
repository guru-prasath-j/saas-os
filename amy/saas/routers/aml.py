"""AML Monitoring Module (Phase 2) routes — plain JSON endpoints, no UI.

Illustrative/simulated detection only — see amy/finance/aml_engine.py's
module docstring. Case opening is ungated; escalating a case or requesting
a SAR draft parks in the normal Approval Inbox (GET/POST
/api/automation/approvals...), same pipeline as the Fraud Detection module.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import User
from ..deps import current_user
from .automation import _ctx
from .finance import _finance_db

router = APIRouter()

_PATCHABLE_STATUSES = ("investigating", "closed")


class CasePatch(BaseModel):
    status: str


@router.post("/api/finance/aml/accounts/{aid}/scan")
def scan_account_for_aml(aid: str, user: User = Depends(current_user)):
    """Runs the four typology detectors and opens/reconfirms a case per
    trigger. No LLM involved — pure rule-based (amy/finance/aml_engine.py)."""
    from ...finance.aml_engine import investigate_account

    fe = _finance_db(user)
    try:
        if fe.get_account(aid) is None:
            raise HTTPException(status_code=404, detail="account not found")
    finally:
        fe.close()

    cdb, ctx = _ctx(user, with_llm=False)
    try:
        return {"cases": investigate_account(ctx, aid)}
    finally:
        cdb.close()


@router.get("/api/finance/aml/cases")
def list_aml_cases(status: str | None = None, account_id: str | None = None,
                   typology: str | None = None, limit: int = 100,
                   user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        return {"cases": fe.list_aml_cases(status=status, account_id=account_id,
                                           typology=typology, limit=limit)}
    finally:
        fe.close()


@router.get("/api/finance/aml/cases/{case_id}")
def get_aml_case(case_id: str, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        case = fe.get_aml_case(case_id)
    finally:
        fe.close()
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    return case


@router.patch("/api/finance/aml/cases/{case_id}")
def update_aml_case(case_id: str, body: CasePatch, user: User = Depends(current_user)):
    if body.status not in _PATCHABLE_STATUSES:
        raise HTTPException(status_code=400,
                            detail=f"status must be one of {_PATCHABLE_STATUSES} — "
                                   "use POST .../escalate to escalate a case")
    fe = _finance_db(user)
    try:
        if fe.get_aml_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        fe.update_aml_case_status(case_id, body.status)
    finally:
        fe.close()
    return {"ok": True}


@router.post("/api/finance/aml/cases/{case_id}/escalate")
def escalate_aml_case(case_id: str, user: User = Depends(current_user)):
    from ...finance.aml_engine import escalate_case

    cdb, ctx = _ctx(user, with_llm=False)
    try:
        try:
            return escalate_case(ctx, case_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        cdb.close()


@router.post("/api/finance/aml/cases/{case_id}/sar-draft")
def request_aml_sar_draft(case_id: str, user: User = Depends(current_user)):
    from ...finance.aml_engine import generate_sar_draft

    cdb, ctx = _ctx(user, with_llm=False)
    try:
        try:
            return generate_sar_draft(ctx, case_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        cdb.close()
