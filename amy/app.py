from __future__ import annotations
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response, FileResponse
from pydantic import BaseModel
from .engine import get_engine
from . import dashboard_data, config, prefs, security, captures as captures_mod
from .auth import require_auth

app = FastAPI(title=config.APP_NAME, version="1.0.0")
DASH = Path(__file__).parent / "dashboard" / "index.html"


class Query(BaseModel):
    text: str
    channel: str = "text"

class Confirm(BaseModel):
    id: str

class VoicePick(BaseModel):
    id: str

class TTSReq(BaseModel):
    text: str
    voice: str = ""


def _dump(r):
    return {
        "intent": r.intent, "answer": r.answer, "voice_safe": r.voice_safe,
        "sources": r.sources, "sensitive": r.sensitive, "model": r.model,
        "route": r.route, "refusal": r.refusal,
        "needs_confirmation": r.needs_confirmation, "proposal": r.proposal,
    }


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(
        DASH.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )

@app.get("/api/meta")
def meta():
    return {
        "app": config.APP_NAME, "tagline": config.TAGLINE,
        "mode": config.MODE, "public": config.PUBLIC,
        "mode_label": "Public Demo Mode" if config.PUBLIC else "Personal Mode",
        "features": config.FEATURES, "allowed_agents": config.ALLOWED_AGENTS,
        "voices": prefs.load_voices().get("voices", []),
        "voice": prefs.current_voice(),
        "voice_meta": prefs.voice_meta(prefs.current_voice()),
    }

@app.get("/api/voices")
def voices():
    return {"voices": prefs.load_voices().get("voices", []), "current": prefs.current_voice()}

@app.post("/api/voice")
def set_voice(v: VoicePick):
    ok = prefs.set_voice(v.id)
    return {"ok": ok, "current": prefs.current_voice(), "voice_meta": prefs.voice_meta(prefs.current_voice())}

@app.get("/api/tts/status")
def tts_status():
    from . import tts
    have = {v["id"]: tts.available(v["id"]) for v in prefs.load_voices().get("voices", [])}
    return {"ready": any(have.values()), "voices": have}

@app.post("/api/tts")
def tts_synth(req: TTSReq):
    from . import tts
    vid = req.voice or prefs.current_voice()
    try:
        audio = tts.synth(req.text, vid)
        return Response(audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": "tts_unavailable", "detail": str(e)}, status_code=503)

@app.get("/api/health")
def health():
    h = get_engine().health(); h["mode"] = config.MODE; h["app"] = config.APP_NAME
    return h


@app.get("/api/settings/vault")
def get_vault_settings():
    """Current vault sync settings + the resolved active folder."""
    from .vault_settings import VaultSettings
    from . import engine as engmod
    return VaultSettings(engmod._vault_settings_path()).status(default=config.VAULT)


class VaultSettingsReq(BaseModel):
    cloud_sync: Optional[bool] = None
    cloud_path: Optional[str] = None
    local_path: Optional[str] = None


@app.post("/api/settings/vault")
def set_vault_settings(req: VaultSettingsReq, _=Depends(require_auth)):
    """Update vault sync settings, then rebuild the engine against the new folder
    (ON = cloud-synced Obsidian folder, OFF = local folder)."""
    from .vault_settings import VaultSettings
    from . import engine as engmod
    vs = VaultSettings(engmod._vault_settings_path())
    vs.set(cloud_sync=req.cloud_sync, cloud_path=req.cloud_path, local_path=req.local_path)
    engmod.reset_engine()   # apply the new vault folder immediately
    return vs.status(default=config.VAULT)

@app.get("/api/stats")
def stats():
    return get_engine().stats()

@app.get("/api/dashboard")
def dashboard_api():
    return dashboard_data.build(get_engine().notes)

@app.post("/api/query")
def query(q: Query, _=Depends(require_auth)):
    return JSONResponse(_dump(get_engine().ask(q.text, channel=q.channel)))

@app.post("/api/confirm")
def confirm(c: Confirm, _=Depends(require_auth)):
    if config.PUBLIC:
        return JSONResponse({"intent": "blocked", "answer": security.BLOCKED_MSG, "route": "public-blocked"})
    return JSONResponse(_dump(get_engine().confirm(c.id)))

@app.post("/api/captures")
async def create_capture(
    file: UploadFile = File(...),
    taken_at: Optional[str] = Form(None),
    lat: Optional[float] = Form(None),
    lon: Optional[float] = Form(None),
    source: str = Form("mobile"),
    note: str = Form(""),
    tags: str = Form(""),
    _=Depends(require_auth),
):
    """Ingest a photo from the mobile app: store image, caption + OCR, write a
    capture note into the vault (08_Captures/), and index it for Amy."""
    if config.PUBLIC:
        return JSONResponse({"error": "disabled", "detail": "captures are disabled in public mode"}, status_code=403)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    res = captures_mod.ingest(
        data, filename=file.filename or "", content_type=file.content_type,
        taken_at=taken_at, lat=lat, lon=lon, source=source, note=note, tags=tag_list,
    )
    if not res.duplicate:
        get_engine().add_capture_note(res.note_path)
    return {
        "ok": True, "duplicate": res.duplicate, "note_path": res.note_path,
        "image_path": res.image_path, "title": res.title, "caption": res.caption,
        "ocr": res.ocr, "place": res.place, "created": res.created, "hash": res.hash,
    }


@app.get("/api/captures")
def list_captures(limit: int = 50, _=Depends(require_auth)):
    return {"captures": captures_mod.list_captures(get_engine().notes, limit=limit)}


@app.get("/api/captures/image")
def capture_image(path: str, _=Depends(require_auth)):
    """Serve a stored capture image. `path` is the vault-relative image path."""
    if not path.startswith(captures_mod.CAPTURES_REL + "/"):
        raise HTTPException(status_code=400, detail="invalid path")
    abs_path = (Path(config.VAULT) / path).resolve()
    if Path(config.VAULT).resolve() not in abs_path.parents or not abs_path.exists():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(str(abs_path))


@app.get("/api/graph/viz")
def graph_viz(_=Depends(require_auth)):
    """Return nodes and edges representing the vault's note relationships for visualization."""
    import re
    engine = get_engine()
    notes = engine.notes
    
    nodes = []
    for n in notes:
        nodes.append({
            "id": n.path,
            "title": n.title,
            "domain": n.category or "general",
            "importance": min(10, max(2, len(n.body or "") // 200)),
        })
        
    edges = []
    title_to_path = {n.title.lower(): n.path for n in notes}
    for n in notes:
        src = n.path
        for target in re.findall(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", n.body or ""):
            dst = title_to_path.get(target.strip().lower())
            if dst and dst != src:
                edges.append({
                    "src": src,
                    "dst": dst,
                    "rel_type": "references",
                    "weight": 1.0
                })
                
    from .product.graphviz import to_graph
    return to_graph(nodes, edges)


@app.get("/api/notes")
def list_notes(_=Depends(require_auth)):
    """List all notes in the vault with metadata."""
    engine = get_engine()
    out = []
    for n in engine.notes:
        out.append({
            "path": n.path,
            "title": n.title,
            "category": n.category,
            "owner": n.owner,
            "tags": n.tags,
            "words": len((n.body or "").split()),
        })
    return {"notes": out}


class NoteSaveReq(BaseModel):
    path: str
    body: str


@app.get("/api/notes/content")
def get_note_content(path: str, _=Depends(require_auth)):
    """Get the full content and metadata of a single note."""
    engine = get_engine()
    for n in engine.notes:
        if n.path == path:
            return {
                "path": n.path,
                "title": n.title,
                "category": n.category,
                "owner": n.owner,
                "tags": n.tags,
                "body": n.body,
            }
    raise HTTPException(status_code=404, detail="Note not found")


@app.post("/api/notes/save")
def save_note(req: NoteSaveReq, _=Depends(require_auth)):
    """Save edited content of a note back to disk, preserving frontmatter."""
    if config.PUBLIC:
        return JSONResponse({"error": "disabled", "detail": "writing notes is disabled in public mode"}, status_code=403)
    engine = get_engine()
    abs_path = (Path(engine.vault_path) / req.path).resolve()
    if Path(engine.vault_path).resolve() not in abs_path.parents or not abs_path.exists():
        raise HTTPException(status_code=404, detail="File not found or access denied")
    
    text = abs_path.read_text(encoding="utf-8", errors="ignore")
    fm_part = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm_part = text[:end + 4] + "\n"
    
    # Save to disk
    abs_path.write_text(fm_part + req.body, encoding="utf-8")
    
    # Reload in engine
    engine.add_capture_note(req.path)
    return {"ok": True}


class JobClipReq(BaseModel):
    raw_text: str
    url: Optional[str] = None


@app.post("/api/career/clip")
def clip_job(req: JobClipReq, _=Depends(require_auth)):
    if config.PUBLIC:
        return JSONResponse({"error": "disabled", "detail": "writing is disabled in public mode"}, status_code=403)
    from .intelligence.career import normalizer
    engine = get_engine()
    # Normalize the job
    job_info = normalizer.normalize_job_description(engine.master.classifier.llm, req.raw_text)
    if req.url:
        job_info["url"] = req.url
        
    # Check duplicate
    dup = normalizer.check_duplicate(engine.notes, job_info["title"], job_info["company"], job_info["url"])
    if dup:
        return {"ok": True, "duplicate": True, "note_path": dup.path}
        
    # Save file
    safe_company = "".join(c for c in job_info["company"] if c.isalnum() or c in " -_").strip()
    safe_title = "".join(c for c in job_info["title"] if c.isalnum() or c in " -_").strip()
    note_path = f"06_Job_Search/{safe_company} - {safe_title}.md"
    abs_path = Path(engine.vault_path) / note_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    body = normalizer.generate_job_markdown(job_info)
    abs_path.write_text(body, encoding="utf-8")
    
    # Reload note in engine
    engine.add_capture_note(note_path)
    
    return {"ok": True, "duplicate": False, "note_path": note_path}


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    eng = get_engine()
    if config.AUTH_TOKEN:
        first = await sock.receive_json()
        if first.get("token") != config.AUTH_TOKEN:
            await sock.send_json({"error": "unauthorized"}); await sock.close(); return
    try:
        while True:
            data = await sock.receive_json()
            if data.get("confirm"):
                if config.PUBLIC:
                    await sock.send_json({"intent": "blocked", "answer": security.BLOCKED_MSG, "route": "public-blocked"})
                    continue
                r = eng.confirm(data["confirm"])
            else:
                r = eng.ask(data.get("text", ""), channel=data.get("channel", "text"))
            await sock.send_json(_dump(r))
    except WebSocketDisconnect:
        pass
