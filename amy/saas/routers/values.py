"""Values screening routes (Phase R7A-1).

Route order: exact paths (/api/values/presets, /profiles, /flags) before
parameterized /{pid} and /{fid} paths.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import User
from ..deps import current_user, _collab_db_path
from .. import paths

router = APIRouter()


class ProfileCreate(BaseModel):
    preset_id: str | None = None
    name: str | None = None
    rules: list[dict] | None = None


class ProfilePatch(BaseModel):
    enabled: bool | None = None
    rules: list[dict] | None = None


def _fe(user: "User"):
    from ...finance.engine import FinanceEngine
    return FinanceEngine(str(paths.index_dir(user.id) / "finance.db"))


@router.get("/api/values/presets")
def values_presets(user: User = Depends(current_user)):
    from ...values import list_presets
    return {"presets": list_presets()}


@router.get("/api/values/profiles")
def values_profiles(user: User = Depends(current_user)):
    from ...values import list_profiles
    fe = _fe(user)
    try:
        return {"profiles": list_profiles(fe)}
    finally:
        fe.close()


@router.post("/api/values/profiles")
def values_profile_create(body: ProfileCreate, user: User = Depends(current_user)):
    from ...values import enable_profile
    fe = _fe(user)
    try:
        pid = enable_profile(fe, preset_id=body.preset_id, name=body.name,
                             rules=body.rules)
        return {"id": pid}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        fe.close()


@router.patch("/api/values/profiles/{pid}")
def values_profile_patch(pid: str, body: ProfilePatch,
                         user: User = Depends(current_user)):
    from ...values import update_profile
    fe = _fe(user)
    try:
        if not update_profile(fe, pid, enabled=body.enabled, rules=body.rules):
            raise HTTPException(status_code=404, detail="profile not found")
        return {"ok": True}
    finally:
        fe.close()


@router.get("/api/values/flags")
def values_flags(status: str | None = "open", limit: int = 100,
                 user: User = Depends(current_user)):
    from ...values import list_flags
    from ...collab import CollabDB
    cdb = CollabDB(_collab_db_path(user))
    try:
        return {"flags": list_flags(
            cdb.conn, status=None if status in (None, "", "all") else status,
            limit=limit)}
    finally:
        cdb.close()


@router.post("/api/values/flags/{fid}/dismiss")
def values_flag_dismiss(fid: str, user: User = Depends(current_user)):
    from ...values import set_flag_status
    from ...collab import CollabDB
    cdb = CollabDB(_collab_db_path(user))
    try:
        if not set_flag_status(cdb.conn, fid, "dismissed"):
            raise HTTPException(status_code=404, detail="flag not found")
        return {"ok": True}
    finally:
        cdb.close()
