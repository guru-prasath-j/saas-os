"""Jurisdiction + locale routes (Phase R7B).

Route order: exact paths (/api/jurisdictions/deadlines) BEFORE the
parameterized /api/jurisdictions/{pack_id}.
"""
from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import User, get_db
from ..deps import current_user
from .. import paths

router = APIRouter()


class LocaleBody(BaseModel):
    home_jurisdiction: str | None = None
    active_jurisdictions: list[str] | None = None
    language: str | None = None


def user_jurisdictions(user: "User") -> list[str]:
    """home + active list (deduped, home first)."""
    home = (user.home_jurisdiction or "india").lower()
    active = [j.strip().lower()
              for j in (user.active_jurisdictions or "").split(",") if j.strip()]
    return list(dict.fromkeys([home] + active))


def home_pack(user: "User") -> dict:
    from ...jurisdictions import load_pack, PackError
    try:
        return load_pack((user.home_jurisdiction or "india").lower())
    except PackError:
        return load_pack("india")


# --- packs ---------------------------------------------------------------------

@router.get("/api/jurisdictions")
def jurisdictions_list(user: User = Depends(current_user)):
    from ...jurisdictions import list_packs
    return {"packs": list_packs(),
            "home": (user.home_jurisdiction or "india").lower(),
            "active": user_jurisdictions(user)}


@router.get("/api/jurisdictions/deadlines")
def jurisdiction_deadlines(days: int = 90, user: User = Depends(current_user)):
    """Upcoming obligation/compliance dates across ALL the user's active
    jurisdictions, each with its pack disclaimer."""
    from ...jurisdictions import load_pack, upcoming_deadlines, PackError
    today = _dt.date.today()
    out, disclaimers = [], {}
    for jid in user_jurisdictions(user):
        try:
            pack = load_pack(jid)
        except PackError:
            continue
        out.extend(upcoming_deadlines(pack, after=today, horizon_days=days))
        disclaimers[jid] = pack["disclaimer"]
    out.sort(key=lambda x: x["date"])
    return {"as_of": today.isoformat(), "horizon_days": days,
            "deadlines": out, "disclaimers": disclaimers}


@router.get("/api/jurisdictions/{pack_id}")
def jurisdiction_pack(pack_id: str, user: User = Depends(current_user)):
    from ...jurisdictions import load_pack, PackError
    try:
        return load_pack(pack_id.lower())
    except PackError as e:
        raise HTTPException(status_code=404, detail=str(e))


# --- locale settings --------------------------------------------------------------

@router.get("/api/settings/locale")
def get_locale(user: User = Depends(current_user)):
    pack = home_pack(user)
    return {
        "home_jurisdiction": (user.home_jurisdiction or "india").lower(),
        "active_jurisdictions": user_jurisdictions(user),
        "language": user.language,
        "currency": pack["currency"],
    }


@router.post("/api/settings/locale")
def set_locale(body: LocaleBody, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    from ...jurisdictions import load_pack, PackError
    row = db.get(User, user.id)
    if body.home_jurisdiction is not None:
        jid = body.home_jurisdiction.lower()
        try:
            load_pack(jid)
        except PackError:
            raise HTTPException(status_code=400, detail=f"unknown jurisdiction {jid!r}")
        row.home_jurisdiction = jid
    if body.active_jurisdictions is not None:
        cleaned = []
        for jid in body.active_jurisdictions:
            jid = jid.lower()
            try:
                load_pack(jid)
            except PackError:
                raise HTTPException(status_code=400,
                                    detail=f"unknown jurisdiction {jid!r}")
            cleaned.append(jid)
        row.active_jurisdictions = ",".join(dict.fromkeys(cleaned))
    if body.language is not None:
        row.language = body.language.strip() or None
    db.commit()
    return get_locale(row)


# --- multi-currency overview ---------------------------------------------------------

@router.get("/api/finance/overview/fx")
def overview_fx(user: User = Depends(current_user)):
    """Totals per native currency and per jurisdiction, converted to the
    user's base (home-pack) currency. Custodial accounts stay excluded from
    income/spend, matching the engine's rule."""
    from ...finance.engine import FinanceEngine
    from ...fx import FxConverter
    pack = home_pack(user)
    base = pack["currency"]["code"]
    home_id = (user.home_jurisdiction or "india").lower()

    from ...fx import multi_currency_summary
    fe = FinanceEngine(str(paths.index_dir(user.id) / "finance.db"))
    try:
        summary = multi_currency_summary(
            fe, base, home_id, FxConverter(cache_dir=paths.SAAS_DATA))
    finally:
        fe.close()
    summary["fx_note"] = ("converted with daily-cached rates (seed fallback); "
                          "see amy/jurisdictions/fx_seed.json")
    return summary
