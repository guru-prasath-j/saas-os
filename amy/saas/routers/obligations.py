"""Obligation routes (Phase R7A-2).

Route order: exact paths (/api/obligations, /api/obligations/presets,
/api/obligations/activate) BEFORE parameterized /{oid} paths.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import User
from ..deps import current_user
from .. import paths
from .jurisdictions import user_jurisdictions

router = APIRouter()


class ActivateBody(BaseModel):
    jurisdiction: str
    preset_id: str
    config: dict = {}


class ConfigBody(BaseModel):
    config: dict


def _fe(user: "User"):
    from ...finance.engine import FinanceEngine
    return FinanceEngine(str(paths.index_dir(user.id) / "finance.db"))


@router.get("/api/obligations")
def obligations_status(user: User = Depends(current_user)):
    """Every active obligation with its computed status, rules, disclaimer."""
    from ...obligations import all_statuses
    fe = _fe(user)
    try:
        return {"obligations": all_statuses(fe)}
    finally:
        fe.close()


@router.get("/api/obligations/presets")
def obligations_presets(user: User = Depends(current_user)):
    """Presets available across the user's active jurisdictions."""
    from ...jurisdictions import load_pack, list_obligation_presets, PackError
    out = []
    for jid in user_jurisdictions(user):
        try:
            pack = load_pack(jid)
        except PackError:
            continue
        for p in list_obligation_presets(pack):
            out.append({"jurisdiction": jid, "preset_id": p["id"],
                        "name": p["name"], "kind": p["kind"],
                        "description": p.get("description", ""),
                        "currency": pack["currency"]["code"]})
    return {"presets": out}


@router.post("/api/obligations/activate")
def obligations_activate(body: ActivateBody, user: User = Depends(current_user)):
    from ...obligations import activate
    fe = _fe(user)
    try:
        oid = activate(fe, body.jurisdiction, body.preset_id, body.config)
        return {"id": oid}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        fe.close()


@router.patch("/api/obligations/{oid}")
def obligations_config(oid: str, body: ConfigBody,
                       user: User = Depends(current_user)):
    from ...obligations import update_config
    fe = _fe(user)
    try:
        if not update_config(fe, oid, body.config):
            raise HTTPException(status_code=404, detail="obligation not found")
        return {"ok": True}
    finally:
        fe.close()


@router.post("/api/obligations/{oid}/deactivate")
def obligations_deactivate(oid: str, user: User = Depends(current_user)):
    from ...obligations import deactivate
    fe = _fe(user)
    try:
        if not deactivate(fe, oid):
            raise HTTPException(status_code=404, detail="obligation not found")
        return {"ok": True}
    finally:
        fe.close()
