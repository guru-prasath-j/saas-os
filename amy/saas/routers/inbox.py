"""Universal Approval Inbox API (/api/inbox) — CONTEXT_PLAN C6.

The Approval Inbox generalized beyond internal agents: ANY external system
(whatsapp_brain, a calendar bot, a mail drafter) can park a proposed action or
draft as a tier-2 approval, then poll for the human's decision and act only on
approved rows. The contract:

    POST /api/inbox/propose      → {approval_id}          (parks tier 2)
    GET  /api/inbox/pending      ?source=whatsapp_brain   (what's waiting)
    GET  /api/inbox/decisions    ?since=ISO&source=…      (approved/rejected/expired)

Nothing executes server-side on approval (the executor is an acknowledging
no-op) — execution authority stays with the proposing system, consent stays
with the human, and the audit trail lives in the same approvals ledger every
other agent uses."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import User
from ..deps import current_user, _collab_db_path
from .. import paths

router = APIRouter()


def _ctx(user: User, cdb):
    from ...automation.jobs import build_ctx
    return build_ctx(user.id, user.email, cdb, paths.index_dir(user.id),
                     llm_router=None)


class ProposeBody(BaseModel):
    title: str
    body: str = ""
    source: str = "external"
    payload: dict = {}
    reasoning: str = ""
    dedup_key: str | None = None
    expires_at: str | None = None


@router.post("/api/inbox/propose")
def propose(body: ProposeBody, user: User = Depends(current_user)):
    if not body.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    from ...automation.executors import submit_action
    from ...collab import CollabDB
    cdb = CollabDB(_collab_db_path(user))
    try:
        out = submit_action(
            _ctx(user, cdb), tier=2, action_type="external_draft",
            title=body.title, body=body.body, payload=body.payload,
            source=body.source or "external", reasoning=body.reasoning,
            risk="write", dedup_key=body.dedup_key,
            expires_at=body.expires_at)
        return out
    finally:
        cdb.close()


def _rows(user: User, where: str, params: list) -> list[dict]:
    from ...collab import CollabDB
    from ...automation.store import AutomationStore
    cdb = CollabDB(_collab_db_path(user))
    try:
        store = AutomationStore(cdb)   # ensures tables exist
        try:
            store.expire_stale()
        except Exception:
            pass
        rows = cdb.conn.execute(
            "SELECT id, created_at, decided_at, title, body, payload, status,"
            " source, reasoning FROM approvals"
            f" WHERE action_type='external_draft' {where}"
            " ORDER BY created_at DESC LIMIT 200", params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.get("payload") or "{}")
            out.append(d)
        return out
    finally:
        cdb.close()


@router.get("/api/inbox/pending")
def pending(source: str | None = None, user: User = Depends(current_user)):
    where, params = " AND status='pending'", []
    if source:
        where += " AND source=?"
        params.append(source)
    return {"pending": _rows(user, where, params)}


@router.get("/api/inbox/decisions")
def decisions(since: str | None = None, source: str | None = None,
              user: User = Depends(current_user)):
    where = " AND status IN ('executed','rejected','expired','failed')"
    params: list = []
    if since:
        where += " AND decided_at >= ?"
        params.append(since)
    if source:
        where += " AND source=?"
        params.append(source)
    return {"decisions": _rows(user, where, params)}
