"""Captures (image/text) and career job-clip routes."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..db import User
from .. import paths, tenancy
from ..deps import current_user, _engine_for, _user_key

router = APIRouter()


class JobClipReq(BaseModel):
    raw_text: str
    url: str | None = None


@router.post("/api/captures")
async def create_capture(
    file: UploadFile = File(...),
    taken_at: str | None = Form(None),
    lat: float | None = Form(None),
    lon: float | None = Form(None),
    source: str = Form("mobile"),
    note: str = Form(""),
    tags: str = Form(""),
    link_disbursement_txn: str | None = Form(None),
    user: User = Depends(current_user),
):
    from ... import captures as captures_mod
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    res = captures_mod.ingest(
        data, filename=file.filename or "", content_type=file.content_type,
        taken_at=taken_at, lat=lat, lon=lon, source=source, note=note, tags=tag_list,
        vault=tenancy.resolve_vault_dir(user.id), openai_api_key=_user_key(user),
    )
    if not res.duplicate:
        _engine_for(user).add_capture_note(res.note_path)
        # journal it: capture.added is already understood by MemoryWriter
        # (daily-note entry + atomic 09_Memory note) — fire-and-forget, a
        # journaling failure must never fail the upload that already happened.
        try:
            from ...collab import CollabDB
            from ...events.store import EventStore, CAPTURE_ADDED
            from ..deps import _collab_db_path, _journal_user
            cdb = CollabDB(_collab_db_path(user))
            try:
                EventStore(cdb).emit(CAPTURE_ADDED, {
                    "title": res.title, "caption": res.caption,
                    "place": res.place, "note_path": res.note_path,
                    "image_path": res.image_path,
                    "ocr": (res.ocr or "")[:500], "source": source,
                }, source="captures")
            finally:
                cdb.close()
            _journal_user(user)   # catch up 00_Daily/09_Memory immediately
        except Exception:
            pass

    # Narrowly-scoped: link this screenshot to a specific custodial
    # disbursement transaction, if the caller (share-intent flow) asked for
    # it. No general-purpose "attach to any entity" system — just this one.
    if link_disbursement_txn:
        from ...finance.engine import FinanceEngine
        fe = FinanceEngine(str(paths.index_dir(user.id) / "finance.db"))
        try:
            fe.conn.execute(
                "UPDATE transactions SET screenshot_path=? WHERE id=?",
                (res.image_path, link_disbursement_txn))
            fe.conn.commit()
        finally:
            fe.close()

    return {
        "ok": True, "duplicate": res.duplicate, "note_path": res.note_path,
        "image_path": res.image_path, "title": res.title, "caption": res.caption,
        "ocr": res.ocr, "place": res.place, "created": res.created, "hash": res.hash,
    }


@router.get("/api/captures")
def captures_list(limit: int = 50, user: User = Depends(current_user)):
    from ... import captures as captures_mod
    eng = _engine_for(user)
    return {"captures": captures_mod.list_captures(eng.notes, limit=limit)}


@router.get("/api/captures/image")
def capture_image(path: str, user: User = Depends(current_user)):
    from ... import captures as captures_mod
    if not path.startswith(captures_mod.CAPTURES_REL + "/"):
        raise HTTPException(status_code=400, detail="invalid path")
    vroot = tenancy.resolve_vault_dir(user.id).resolve()
    abs_path = (vroot / path).resolve()
    if vroot not in abs_path.parents or not abs_path.exists():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(str(abs_path))


@router.post("/api/career/clip")
def saas_clip_job(req: JobClipReq, user: User = Depends(current_user)):
    from ...intelligence.career import normalizer
    from ...llm import LLMRouter
    engine = _engine_for(user)
    key = _user_key(user)
    llm = LLMRouter(openai_api_key=key, use_global_keys=False)

    job_info = normalizer.normalize_job_description(llm, req.raw_text)
    if req.url:
        job_info["url"] = req.url

    dup = normalizer.check_duplicate(engine.notes, job_info["title"],
                                     job_info["company"], job_info["url"])
    if dup:
        return {"ok": True, "duplicate": True, "note_path": dup.path}

    safe_company = "".join(c for c in job_info["company"]
                           if c.isalnum() or c in " -_").strip()
    safe_title = "".join(c for c in job_info["title"]
                         if c.isalnum() or c in " -_").strip()
    note_path = f"06_Job_Search/{safe_company} - {safe_title}.md"
    abs_path = tenancy.resolve_vault_dir(user.id) / note_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(normalizer.generate_job_markdown(job_info), encoding="utf-8")

    engine.add_capture_note(note_path)
    return {"ok": True, "duplicate": False, "note_path": note_path}
