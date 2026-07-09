"""Business entity routes — register any side business via a form, then get
a generic Ledger (Accountant/Auditor) + Compliance pipeline with zero new
code per business. See BUSINESS.md at the repo root for the full design.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from ..db import User
from ..deps import current_user, _collab_db_path, _user_key

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _finance_db(user: "User"):
    from .. import paths
    from ...finance import FinanceEngine
    return FinanceEngine(str(paths.index_dir(user.id) / "finance.db"))


def _open_collab(user: "User"):
    from ...collab import CollabDB
    return CollabDB(_collab_db_path(user))


def _emit_biz(user: "User", event_type: str, payload: dict) -> str | None:
    """Fire-and-forget business event. Returns the event id, or None if it
    failed — a bad event must never break the route, but callers that need
    a source_event_id for provenance should treat None as "no event" and
    surface a 500 rather than write a row with no provenance.

    Uses amy.events.factory.get_events() (Part 0 / quirk 20 fix) — this used
    to be a bare EventStore, which meant FINANCE_LEDGER_ENTRY_POSTED emitted
    here never reached the compliance reactive agent (it subscribes to that
    exact event type in amy/agents/reactive.py). Posting a ledger entry via
    this router silently skipped the compliance review that posting the same
    entry through the finance router already triggers."""
    try:
        from ...events.factory import get_events
        from .. import paths
        cdb = _open_collab(user)
        try:
            es = get_events(user.id, cdb, index_dir=paths.index_dir(user.id),
                            user_email=user.email)
            return es.emit(event_type, payload, source="business")
        finally:
            cdb.close()
    except Exception:
        return None


def _get_entity_or_404(fe, entity_id: str) -> dict:
    from ...finance.business import entities
    entity = entities.get_entity(fe, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="business entity not found")
    return entity


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class EntityBody(BaseModel):
    name: str
    pan: str | None = None
    gstin: str | None = None
    constitution: str = "proprietorship"
    registration_state: str | None = None
    financial_year: str | None = None
    tax_regime: str | None = None
    holds_depreciable_assets: bool = False
    tracking_closeness: str = "loose"


class EntityUpdateBody(BaseModel):
    name: str | None = None
    pan: str | None = None
    gstin: str | None = None
    constitution: str | None = None
    registration_state: str | None = None
    financial_year: str | None = None
    tax_regime: str | None = None
    holds_depreciable_assets: bool | None = None
    tracking_closeness: str | None = None


class LedgerEntryUpdateBody(BaseModel):
    date: str | None = None
    amount: float | None = None
    description: str | None = None
    category: str | None = None


class RateUpdateBody(BaseModel):
    value: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    source_note: str | None = None


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

@router.post("/api/business/entities")
def create_entity(body: EntityBody, user: User = Depends(current_user)):
    from ...events.store import BUSINESS_ENTITY_CREATED
    from ...finance.business import entities
    fe = _finance_db(user)
    try:
        try:
            eid = entities.create_entity(fe, **body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        _emit_biz(user, BUSINESS_ENTITY_CREATED, {"entity_id": eid, "name": body.name})
        return entities.get_entity(fe, eid)
    finally:
        fe.close()


@router.get("/api/business/entities")
def list_entities(user: User = Depends(current_user)):
    from ...finance.business import entities
    fe = _finance_db(user)
    try:
        return {"entities": entities.list_entities(fe)}
    finally:
        fe.close()


@router.get("/api/business/entities/{entity_id}")
def get_entity(entity_id: str, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        return _get_entity_or_404(fe, entity_id)
    finally:
        fe.close()


@router.patch("/api/business/entities/{entity_id}")
def update_entity(entity_id: str, body: EntityUpdateBody, user: User = Depends(current_user)):
    from ...finance.business import entities
    fe = _finance_db(user)
    try:
        _get_entity_or_404(fe, entity_id)
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        try:
            entities.update_entity(fe, entity_id, **fields)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return entities.get_entity(fe, entity_id)
    finally:
        fe.close()


@router.delete("/api/business/entities/{entity_id}")
def delete_entity(entity_id: str, user: User = Depends(current_user)):
    from ...finance.business import entities
    fe = _finance_db(user)
    try:
        _get_entity_or_404(fe, entity_id)
        entities.delete_entity(fe, entity_id)
        return {"deleted": True}
    finally:
        fe.close()


# ---------------------------------------------------------------------------
# Ledger (Accountant)
# ---------------------------------------------------------------------------

@router.post("/api/business/entities/{entity_id}/ledger/upload")
async def upload_ledger_document(entity_id: str, file: UploadFile = File(...),
                                 user: User = Depends(current_user)):
    """Extract structured ledger entries from an uploaded document and post
    them — auto-post threshold gated by the entity's tracking_closeness."""
    from ...events.store import FINANCE_LEDGER_ENTRY_POSTED
    from ...finance.business import accountant
    from ...llm import LLMRouter

    fe = _finance_db(user)
    try:
        entity = _get_entity_or_404(fe, entity_id)
        raw = await file.read()
        filename = file.filename or ""
        llm = LLMRouter(openai_api_key=_user_key(user), use_global_keys=True)
        try:
            extracted = accountant.extract_ledger_entries(raw, filename, llm)
        except accountant.UnsupportedDocumentFormat as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        threshold = accountant.auto_post_threshold(entity["tracking_closeness"])
        posted = []
        for entry in extracted:
            eid = _emit_biz(user, FINANCE_LEDGER_ENTRY_POSTED, {
                "entity_id": entity_id, "entity_name": entity["name"],
                "amount": entry["amount"], "date": entry["date"],
            })
            if eid is None:
                raise HTTPException(status_code=500, detail="could not record source event")
            lid = fe.add_ledger_entry(
                business_entity_id=entity_id, date=entry["date"], amount=entry["amount"],
                source_event_id=eid, description=entry["description"],
                category=entry["category"], source_document=filename,
                confidence=entry["confidence"], posted_by="accountant")
            posted.append(fe.get_ledger_entry(lid))

        below_threshold = sum(1 for e in extracted if e["confidence"] < threshold)
        return {"posted": posted, "count": len(posted),
                "needs_review_count": below_threshold, "auto_post_threshold": threshold}
    finally:
        fe.close()


@router.get("/api/business/entities/{entity_id}/ledger")
def list_ledger_entries(entity_id: str, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        _get_entity_or_404(fe, entity_id)
        return {"entries": fe.list_ledger_entries(entity_id)}
    finally:
        fe.close()


@router.patch("/api/business/entities/{entity_id}/ledger/{entry_id}")
def update_ledger_entry(entity_id: str, entry_id: str, body: LedgerEntryUpdateBody,
                        user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        _get_entity_or_404(fe, entity_id)
        entry = fe.get_ledger_entry(entry_id)
        if entry is None or entry["business_entity_id"] != entity_id:
            raise HTTPException(status_code=404, detail="ledger entry not found")
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        fields["posted_by"] = "manual"
        fe.update_ledger_entry(entry_id, **fields)
        return fe.get_ledger_entry(entry_id)
    finally:
        fe.close()


@router.delete("/api/business/entities/{entity_id}/ledger/{entry_id}")
def delete_ledger_entry(entity_id: str, entry_id: str, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        _get_entity_or_404(fe, entity_id)
        entry = fe.get_ledger_entry(entry_id)
        if entry is None or entry["business_entity_id"] != entity_id:
            raise HTTPException(status_code=404, detail="ledger entry not found")
        fe.delete_ledger_entry(entry_id)
        return {"deleted": True}
    finally:
        fe.close()


# ---------------------------------------------------------------------------
# Auditor
# ---------------------------------------------------------------------------

@router.post("/api/business/entities/{entity_id}/ledger/audit")
async def audit_ledger(entity_id: str, file: UploadFile = File(...),
                       user: User = Depends(current_user)):
    """Read-only fidelity check of posted ledger entries against a source
    document — only runs when tracking_closeness == 'close'."""
    from ...finance.business import accountant, auditor
    from ...events.store import FINANCE_LEDGER_AUDITED

    fe = _finance_db(user)
    try:
        entity = _get_entity_or_404(fe, entity_id)
        if entity["tracking_closeness"] != "close":
            raise HTTPException(
                status_code=400,
                detail="Auditor only runs for closely-managed entities "
                       "(set tracking_closeness to 'close' to enable it).")
        raw = await file.read()
        filename = file.filename or ""
        fmt = accountant._detect_format(raw, filename)
        if fmt == "image":
            raise HTTPException(
                status_code=400,
                detail="Screenshot/photo uploads aren't supported yet — "
                       "please convert this document to PDF or CSV/XLS first.")
        source_text = (accountant._pdf_to_text(raw) if fmt == "pdf"
                       else accountant._spreadsheet_to_text(raw, filename, fmt))
        result = auditor.run_audit(fe, entity_id, source_text)
        _emit_biz(user, FINANCE_LEDGER_AUDITED, {
            "entity_id": entity_id, "entity_name": entity["name"],
            "issues": len(result["issues"]), "entries_checked": result["entries_checked"],
        })
        return result
    finally:
        fe.close()


# ---------------------------------------------------------------------------
# Compliance
# ---------------------------------------------------------------------------

@router.post("/api/business/entities/{entity_id}/compliance/run")
def run_compliance(entity_id: str, user: User = Depends(current_user)):
    from ...finance.business import compliance
    from ...events.store import FINANCE_COMPLIANCE_SUGGESTED
    from ...llm import LLMRouter

    fe = _finance_db(user)
    try:
        entity = _get_entity_or_404(fe, entity_id)
        llm = LLMRouter(openai_api_key=_user_key(user), use_global_keys=True)
        suggestions = compliance.generate_suggestions(fe, entity, llm)
        saved = []
        for s in suggestions:
            eid = _emit_biz(user, FINANCE_COMPLIANCE_SUGGESTED, {
                "entity_id": entity_id, "entity_name": entity["name"],
                "ledger_entry_id": s["ledger_entry_id"],
                "suggestion_type": s["suggestion_type"],
            })
            if eid is None:
                continue  # skip rather than write a suggestion with no provenance
            sid = fe.add_compliance_suggestion(
                business_entity_id=entity_id, ledger_entry_id=s["ledger_entry_id"],
                source_event_id=eid, suggestion_type=s["suggestion_type"],
                reasoning=s["reasoning"], citation=s["citation"],
                rate_used=s["rate_used"], ca_disclaimer=s["ca_disclaimer"],
                routed_sensitive=s["routed_sensitive"])
            saved.append(sid)
        return {"suggestions": fe.list_compliance_suggestions(entity_id),
                "new_count": len(saved)}
    finally:
        fe.close()


@router.get("/api/business/entities/{entity_id}/compliance")
def list_compliance(entity_id: str, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        _get_entity_or_404(fe, entity_id)
        return {"suggestions": fe.list_compliance_suggestions(entity_id)}
    finally:
        fe.close()


# ---------------------------------------------------------------------------
# Rate table (maintenance)
# ---------------------------------------------------------------------------

@router.get("/api/business/rates")
def list_rates(user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        return {"rates": fe.list_rates()}
    finally:
        fe.close()


@router.patch("/api/business/rates/{rate_id}")
def update_rate(rate_id: str, body: RateUpdateBody, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        if not fields:
            raise HTTPException(status_code=422, detail="no fields to update")
        ok = fe.update_rate(rate_id, **fields)
        if not ok:
            raise HTTPException(status_code=404, detail="rate not found")
        return {"updated": True}
    finally:
        fe.close()
