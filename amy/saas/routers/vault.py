"""Vault management, import, notes, basic query, PKOS, and tags routes."""
from __future__ import annotations

import io
import uuid as _uuid
import zipfile
from collections import Counter

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..db import User, ImportJob, get_db
from .. import paths, imports, tenancy
from ..deps import current_user, _engine_for, _user_key, Query

router = APIRouter()


def _dump(r):
    return {
        "intent": r.intent, "answer": r.answer, "voice_safe": r.voice_safe,
        "sources": r.sources, "sensitive": r.sensitive, "model": r.model,
        "route": r.route, "refusal": r.refusal,
        "needs_confirmation": r.needs_confirmation, "proposal": r.proposal,
    }


@router.post("/api/vault/import")
async def import_vault(file: UploadFile = File(...), replace: bool = True,
                       user: User = Depends(current_user), db: Session = Depends(get_db)):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    try:
        zipfile.ZipFile(io.BytesIO(data)).testzip()
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="not a valid .zip")

    up = paths.uploads_dir(user.id)
    up.mkdir(parents=True, exist_ok=True)
    zpath = up / f"{_uuid.uuid4().hex}.zip"
    zpath.write_bytes(data)

    job = ImportJob(user_id=user.id, status="pending")
    db.add(job)
    db.commit()
    imports.start(job.id, user.id, str(zpath), replace)
    return {"job_id": job.id, "status": "pending"}


@router.get("/api/vault/import/{job_id}")
def import_status(job_id: str, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    job = db.get(ImportJob, job_id)
    if not job or job.user_id != user.id:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "job_id": job.id, "status": job.status,
        "markdown_notes": job.markdown_notes, "notes_loaded": job.notes_loaded,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


@router.get("/api/vault")
def vault_info(user: User = Depends(current_user)):
    from ...dynamic import discover_domains
    eng = _engine_for(user)
    domains = discover_domains(eng.notes)
    return {
        "notes": len(eng.notes),
        "index_backend": eng.backend,
        "agents": [{"name": d["name"], "folder": d["folder"], "count": d["count"]}
                   for d in domains],
    }


@router.get("/api/notes")
def list_notes(limit: int = 100, offset: int = 0, user: User = Depends(current_user)):
    eng = _engine_for(user)
    page = eng.notes[offset: offset + limit]
    return {
        "total": len(eng.notes),
        "notes": [{"path": n.path, "title": n.title, "tags": n.tags,
                   "sensitive": n.sensitive} for n in page],
    }


@router.get("/api/vault/tree")
def vault_tree(user: User = Depends(current_user)):
    vault = paths.vault_dir(user.id)
    if not vault.exists():
        return {"tree": []}
    tree = []
    for folder in sorted(vault.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        files = [f for f in folder.rglob("*.md") if f.is_file()]
        sub = []
        for sf in sorted(folder.iterdir()):
            if sf.is_dir() and not sf.name.startswith("."):
                sf_files = [f for f in sf.rglob("*.md") if f.is_file()]
                if sf_files:
                    sub.append({"name": sf.name, "count": len(sf_files)})
        tree.append({"name": folder.name, "count": len(files), "children": sub})
    return {"tree": tree}


@router.delete("/api/vault")
def delete_vault(user: User = Depends(current_user)):
    tenancy.delete_user_data(user.id)
    return {"ok": True}


@router.delete("/api/account")
def delete_account(user: User = Depends(current_user), db: Session = Depends(get_db)):
    tenancy.delete_user_data(user.id)
    db.query(ImportJob).filter(ImportJob.user_id == user.id).delete()
    db.delete(db.get(User, user.id))
    db.commit()
    return {"ok": True}


@router.post("/api/query")
def query(q: Query, user: User = Depends(current_user)):
    eng = _engine_for(user)
    return _dump(eng.ask(q.text, channel=q.channel))


@router.get("/api/stats")
def stats(user: User = Depends(current_user)):
    return _engine_for(user).stats()


def _try_custodial_answer(query: str, user: User) -> dict | None:
    """Custodial chat tool: questions about in-trust money (beneficiary names,
    'custodial', 'disburse', …) answer from the finance ledger via a compact
    fact block — local-only LLM (sensitive=True), template fallback. Returns
    None to fall through to the normal vault agents. Never raises."""
    try:
        from ...finance import FinanceEngine
        from ...finance.custodial_ai import chat_keywords_for, chat_context, _tokens
        from ...llm import LLMRouter
        fe = FinanceEngine(str(paths.index_dir(user.id) / "finance.db"))
        try:
            accounts = [a for a in fe.list_accounts()
                        if a.get("account_type") == "custodial"]
            if not accounts:
                return None
            kws = chat_keywords_for(fe, [a["id"] for a in accounts])
            qtoks = _tokens(query)
            hit = any(qt.startswith(kw) or (len(qt) > 3 and kw.startswith(qt))
                      for qt in qtoks for kw in kws)
            if not hit:
                return None
            context = "\n\n".join(chat_context(fe, a) for a in accounts)
            llm = LLMRouter(openai_api_key=_user_key(user), use_global_keys=True)
            answer, model = llm.generate(
                "You are Amy's custodial-accounts agent. Answer the user's "
                "question using ONLY the facts below about money held in "
                "trust. Be brief and specific with numbers (₹). If the facts "
                "don't cover it, say so plainly.",
                query, context=context, sensitive=True)
            if model == "template" or not (answer or "").strip():
                answer = "Here's what the custodial ledger shows:\n\n" + context
            src = ["finance.db · custodial ledger"]
            return {"query": query, "domains": ["custodial"], "answer": answer,
                    "sections": [{"domain": "custodial", "answer": answer,
                                  "sources": src}],
                    "sources": src}
        finally:
            fe.close()
    except Exception:
        return None


@router.post("/api/ask")
def pkos_ask(q: Query, user: User = Depends(current_user)):
    from ...llm import LLMRouter
    from ...pkos import build_pkos
    cust = _try_custodial_answer(q.text, user)
    if cust is not None:
        return cust
    eng = _engine_for(user)
    llm = LLMRouter(openai_api_key=_user_key(user), use_global_keys=False)
    master, _registry, _domains = build_pkos(eng.notes, llm=llm)
    return master.handle(q.text)


@router.get("/api/vault/analyze")
def pkos_analyze(limit: int = 500, user: User = Depends(current_user)):
    from ...pkos import analyze_vault
    eng = _engine_for(user)
    return {"analyses": analyze_vault(eng.notes[:limit])}


@router.get("/api/domains")
def pkos_domains(user: User = Depends(current_user)):
    from ...pkos import detect
    eng = _engine_for(user)
    dm = detect(eng.notes)
    return {"domains": [{"name": d, "notes": len(p)} for d, p in sorted(dm.items())]}


@router.get("/api/tags")
def list_tags(user: User = Depends(current_user)):
    eng = _engine_for(user)
    counter: Counter = Counter()
    for note in eng.notes:
        for tag in (note.tags or []):
            counter[str(tag).lower().strip("#")] += 1
    return {"tags": [{"name": t, "count": c} for t, c in counter.most_common(300)]}
