"""Memory lake routes: journal sync, daily notes, recall, consolidate, heatmap."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from ..db import User
from .. import paths, tenancy
from ..deps import current_user, _engine_for, _collab_db_path, _journal_user

router = APIRouter()


@router.post("/api/memory/sync")
def memory_sync(user: User = Depends(current_user)):
    return _journal_user(user)


@router.get("/api/memory/daily")
def memory_daily(date: str | None = None, user: User = Depends(current_user)):
    import datetime as _dt
    from ...memory import DAILY_DIR
    d = date or _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    path = tenancy.resolve_vault_dir(user.id) / DAILY_DIR / f"{d}.md"
    if not path.exists():
        return {"date": d, "exists": False, "content": ""}
    return {"date": d, "exists": True,
            "content": path.read_text(encoding="utf-8", errors="ignore")}


@router.get("/api/memory/recall")
def memory_recall(q: str, k: int = 3, user: User = Depends(current_user)):
    from ...memory.recall import MemoryRecall
    from ...collab import CollabDB
    eng = _engine_for(user)
    db = CollabDB(_collab_db_path(user))
    try:
        return {"query": q,
                "hits": MemoryRecall(eng.notes, collab_db=db).recall(q, k=k)}
    finally:
        db.close()


@router.post("/api/memory/consolidate")
def memory_consolidate(user: User = Depends(current_user)):
    from ...memory import Consolidator
    return Consolidator(tenancy.resolve_vault_dir(user.id)).weekly()


@router.get("/api/memory/patterns")
def memory_patterns(user: User = Depends(current_user)):
    from ...memory import Consolidator
    return Consolidator(tenancy.resolve_vault_dir(user.id)).patterns()


@router.get("/api/memory/verify")
def memory_verify(user: User = Depends(current_user)):
    from ...memory import VaultReindex
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        return VaultReindex(tenancy.resolve_vault_dir(user.id)).verify(db)
    finally:
        db.close()


@router.post("/api/memory/reindex")
def memory_reindex(user: User = Depends(current_user)):
    from ...memory import VaultReindex
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        rx = VaultReindex(tenancy.resolve_vault_dir(user.id))
        return {"scan": rx.scan(), "decisions": rx.rebuild_decisions(db)}
    finally:
        db.close()


@router.post("/api/memory/log")
def memory_log(user: User = Depends(current_user)):
    return _journal_user(user)


@router.get("/api/memory/index")
def memory_index(user: User = Depends(current_user)):
    from ...memory import DAILY_DIR
    from ...memory.consolidate import WEEKLY_DIR
    vault = tenancy.resolve_vault_dir(user.id)
    files = []
    for folder in (DAILY_DIR, WEEKLY_DIR, "09_Memory"):
        d = vault / folder
        if not d.exists():
            continue
        for f in sorted(d.iterdir(), reverse=True):
            if f.suffix == ".md":
                files.append({
                    "path": f"{folder}/{f.name}",
                    "name": f.stem,
                    "folder": folder,
                    "size": f.stat().st_size,
                    "modified": f.stat().st_mtime,
                })
    return {"files": files}


@router.get("/api/memory/file")
def memory_file(path: str, user: User = Depends(current_user)):
    vault = tenancy.resolve_vault_dir(user.id).resolve()
    target = (vault / path).resolve()
    if vault not in target.parents and target != vault:
        raise HTTPException(status_code=400, detail="invalid path")
    if not target.exists():
        return {"path": path, "exists": False, "content": ""}
    return {"path": path, "exists": True,
            "content": target.read_text(encoding="utf-8", errors="ignore")}


@router.get("/api/memory/heatmap")
def memory_heatmap(days: int = 90, user: User = Depends(current_user)):
    import datetime as _dt
    from ...collab import CollabDB
    since = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    db = CollabDB(_collab_db_path(user))
    try:
        rows = db.conn.execute(
            "SELECT date(ts) as day, COUNT(*) as count "
            "FROM activities WHERE ts>=? GROUP BY day ORDER BY day",
            (since,),
        ).fetchall()
        return {"heatmap": [{"date": r["day"], "count": r["count"]} for r in rows]}
    finally:
        db.close()


# Operational Layer (/api/ops/*) removed — the OperationalLayer façade
# (amy/operational/layer.py + state/connectors/sync/replay/agent/models)
# was a tested but never-wired-to-a-UI backend (no frontend call site ever
# existed); deleted along with its test suite. amy/operational/sensors.py
# stays — it's the shared Sensor base class GmailSensor/GitHubSensor/
# PlaneSensor/JobScoutSensor/LearningFeedSensor all still extend. See
# CLAUDE.md's Operational Layer migration checklist for the full history.
