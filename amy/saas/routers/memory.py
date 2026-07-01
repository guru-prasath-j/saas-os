"""Memory lake routes: journal sync, daily notes, recall, consolidate, heatmap, ops."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from ..db import User
from .. import paths
from ..deps import current_user, _engine_for, _collab_db_path, _journal_user, _connector_dir

router = APIRouter()


@router.post("/api/memory/sync")
def memory_sync(user: User = Depends(current_user)):
    return _journal_user(user)


@router.get("/api/memory/daily")
def memory_daily(date: str | None = None, user: User = Depends(current_user)):
    import datetime as _dt
    from ...memory import DAILY_DIR
    d = date or _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    path = Path(paths.vault_dir(user.id)) / DAILY_DIR / f"{d}.md"
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
    return Consolidator(paths.vault_dir(user.id)).weekly()


@router.get("/api/memory/patterns")
def memory_patterns(user: User = Depends(current_user)):
    from ...memory import Consolidator
    return Consolidator(paths.vault_dir(user.id)).patterns()


@router.get("/api/memory/verify")
def memory_verify(user: User = Depends(current_user)):
    from ...memory import VaultReindex
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        return VaultReindex(paths.vault_dir(user.id)).verify(db)
    finally:
        db.close()


@router.post("/api/memory/reindex")
def memory_reindex(user: User = Depends(current_user)):
    from ...memory import VaultReindex
    from ...collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        rx = VaultReindex(paths.vault_dir(user.id))
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
    vault = Path(paths.vault_dir(user.id))
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
    vault = Path(paths.vault_dir(user.id)).resolve()
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


# --- operational layer -------------------------------------------------------

@router.get("/api/ops/snapshot")
def ops_snapshot(user: User = Depends(current_user)):
    from ...collab import CollabDB
    from ...operational import OperationalLayer
    from ...events import EventStore
    db = CollabDB(_collab_db_path(user))
    try:
        return OperationalLayer(db, EventStore(db),
                                connector_dir=_connector_dir(user)).snapshot()
    finally:
        db.close()


@router.get("/api/ops/connectors")
def ops_connectors(user: User = Depends(current_user)):
    from ...collab import CollabDB
    from ...operational import OperationalLayer
    from ...events import EventStore
    db = CollabDB(_collab_db_path(user))
    try:
        return {"connectors": OperationalLayer(
            db, EventStore(db), connector_dir=_connector_dir(user)).connectors.status()}
    finally:
        db.close()


@router.post("/api/ops/connectors/health")
def ops_connectors_health(user: User = Depends(current_user)):
    from ...collab import CollabDB
    from ...operational import OperationalLayer
    from ...events import EventStore
    db = CollabDB(_collab_db_path(user))
    try:
        return {"health": OperationalLayer(
            db, EventStore(db),
            connector_dir=_connector_dir(user)).connectors.check_all(mode="private")}
    finally:
        db.close()


@router.get("/api/ops/entities")
def ops_entities(kind: str | None = None, source: str | None = None,
                 limit: int = 100, user: User = Depends(current_user)):
    from ...collab import CollabDB
    from ...operational import OperationalLayer
    from ...events import EventStore
    db = CollabDB(_collab_db_path(user))
    try:
        ol = OperationalLayer(db, EventStore(db), connector_dir=_connector_dir(user))
        ents = ol.state.list_entities(kind=kind, source=source, limit=limit)
        return {"entities": [e.to_dict() for e in ents]}
    finally:
        db.close()


@router.post("/api/ops/sync/{kind}")
def ops_sync(kind: str, user: User = Depends(current_user)):
    from ...collab import CollabDB
    from ...operational import OperationalLayer
    from ...events import EventStore
    db = CollabDB(_collab_db_path(user))
    try:
        return OperationalLayer(
            db, EventStore(db),
            connector_dir=_connector_dir(user)).sync.sync_connector(kind, mode="private")
    finally:
        db.close()


@router.get("/api/ops/replay")
def ops_replay(since: str | None = None, types: str | None = None,
               limit: int = 200, user: User = Depends(current_user)):
    from ...collab import CollabDB
    from ...operational import OperationalLayer
    from ...events import EventStore
    db = CollabDB(_collab_db_path(user))
    try:
        tlist = [t.strip() for t in types.split(",")] if types else None
        ol = OperationalLayer(db, EventStore(db), connector_dir=_connector_dir(user))
        return {"events": ol.replay_service.events(
            since_ts=since, types=tlist, limit=limit)}
    finally:
        db.close()
