"""Background vault import.

Phase 2 runs imports in a background thread so large vaults don't block the
request. For production scale, swap this for a real task queue (Celery/RQ/Arq) —
the worker function stays the same, only the dispatch changes.
"""
from __future__ import annotations

import datetime as _dt
import threading
import zipfile
from pathlib import Path

from .db import ImportJob, SessionLocal
from . import paths, tenancy


def _clear_vault(vdir: Path) -> None:
    for p in sorted(vdir.rglob("*"), reverse=True):
        try:
            p.unlink() if p.is_file() else p.rmdir()
        except Exception:
            pass


def _extract(zip_path: Path, vdir: Path, replace: bool) -> int:
    vdir.mkdir(parents=True, exist_ok=True)
    if replace:
        _clear_vault(vdir)
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        root = vdir.resolve()
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            dest = (vdir / member).resolve()
            # zip-slip guard
            if root not in dest.parents and dest != root:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(dest, "wb") as out:
                out.write(src.read())
            if member.lower().endswith(".md"):
                count += 1
    return count


def run_import(job_id: str, user_id: str, zip_path: str, replace: bool = True) -> None:
    db = SessionLocal()
    try:
        job = db.get(ImportJob, job_id)
        if job is None:
            return
        job.status = "running"
        db.commit()

        md = _extract(Path(zip_path), paths.vault_dir(user_id), replace)
        tenancy.invalidate(user_id)
        loaded = tenancy.warm(user_id)   # reload + build index now

        job.markdown_notes = md
        job.notes_loaded = loaded
        job.status = "done"
        job.finished_at = _dt.datetime.now(_dt.timezone.utc)
        db.commit()
    except Exception as e:
        job = db.get(ImportJob, job_id)
        if job:
            job.status = "failed"
            job.error = str(e)[:500]
            job.finished_at = _dt.datetime.now(_dt.timezone.utc)
            db.commit()
    finally:
        db.close()
        try:
            Path(zip_path).unlink()   # tidy the uploaded zip
        except Exception:
            pass


def start(job_id: str, user_id: str, zip_path: str, replace: bool = True) -> None:
    threading.Thread(
        target=run_import, args=(job_id, user_id, zip_path, replace), daemon=True
    ).start()
