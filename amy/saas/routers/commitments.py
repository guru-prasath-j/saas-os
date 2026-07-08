"""Commitments endpoints (/api/commitments) — CONTEXT_PLAN C3.

Deadline-bearing life admin: auto-detected return windows and warranties,
plus manual entries (documents, renewals, anything with a date)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import User
from ..deps import current_user
from .finance import _finance_db

router = APIRouter()


class CommitmentBody(BaseModel):
    kind: str = "custom"
    title: str
    due_date: str
    merchant: str = ""
    amount: float | None = None
    notes: str = ""


class CommitmentPatch(BaseModel):
    status: str | None = None          # open | done | dismissed
    due_date: str | None = None
    notes: str | None = None
    title: str | None = None


@router.post("/api/commitments")
def add_commitment(body: CommitmentBody, user: User = Depends(current_user)):
    from ...commitments import CommitmentEngine, KINDS
    if body.kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {KINDS}")
    if not body.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    fe = _finance_db(user)
    try:
        cid = CommitmentEngine(fe).add(
            body.kind, body.title, body.due_date,
            merchant=body.merchant, amount=body.amount, notes=body.notes)
        return {"id": cid}
    finally:
        fe.close()


@router.get("/api/commitments")
def list_commitments(status: str = "open", user: User = Depends(current_user)):
    from ...commitments import CommitmentEngine
    fe = _finance_db(user)
    try:
        return {"commitments": CommitmentEngine(fe).list(status=status)}
    finally:
        fe.close()


@router.patch("/api/commitments/{cid}")
def update_commitment(cid: str, body: CommitmentPatch,
                      user: User = Depends(current_user)):
    from ...commitments import CommitmentEngine
    if body.status and body.status not in ("open", "done", "dismissed"):
        raise HTTPException(status_code=400, detail="bad status")
    fe = _finance_db(user)
    try:
        if not CommitmentEngine(fe).update(cid, **body.model_dump()):
            raise HTTPException(status_code=404, detail="commitment not found")
        return {"ok": True}
    finally:
        fe.close()


@router.delete("/api/commitments/{cid}")
def delete_commitment(cid: str, user: User = Depends(current_user)):
    from ...commitments import CommitmentEngine
    fe = _finance_db(user)
    try:
        if not CommitmentEngine(fe).delete(cid):
            raise HTTPException(status_code=404, detail="commitment not found")
        return {"ok": True}
    finally:
        fe.close()
