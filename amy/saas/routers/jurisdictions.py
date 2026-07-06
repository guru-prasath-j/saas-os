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

    fe = FinanceEngine(str(paths.index_dir(user.id) / "finance.db"))
    try:
        accounts = {a["id"]: a for a in fe.list_accounts()}
        rows = fe.conn.execute(
            "SELECT account_id, currency, amount, date FROM transactions").fetchall()
    finally:
        fe.close()

    month_start = _dt.date.today().replace(day=1).isoformat()
    fx = FxConverter(cache_dir=paths.SAAS_DATA)

    def _bucket_add(bucket: dict, key: str, amt: float, in_month: bool):
        b = bucket.setdefault(key, {"balance": 0.0, "month_in": 0.0,
                                    "month_out": 0.0})
        b["balance"] += amt
        if in_month:
            b["month_in" if amt > 0 else "month_out"] += abs(amt)

    by_currency: dict[str, dict] = {}     # native amounts per currency
    by_jurisdiction: dict[str, dict] = {} # BASE-converted amounts per pack
    unconvertible: set[str] = set()
    for r in rows:
        acc = accounts.get(r["account_id"]) or {}
        if acc.get("account_type") == "custodial":
            continue   # never counts toward the user's own money
        cur = (r["currency"] or acc.get("currency") or base).upper()
        jur = (acc.get("jurisdiction") or home_id).lower()
        amt = float(r["amount"] or 0)
        in_month = (r["date"] or "") >= month_start
        _bucket_add(by_currency, cur, amt, in_month)
        try:
            _bucket_add(by_jurisdiction, jur, fx.convert(amt, cur, base), in_month)
        except ValueError:
            unconvertible.add(cur)

    currencies = {}
    total_base = 0.0
    for cur, b in by_currency.items():
        out = {k: round(v, 2) for k, v in b.items()}
        try:
            rate = fx.rate(cur, base)
            out["in_base"] = {k: round(v * rate, 2) for k, v in b.items()}
            total_base += out["in_base"]["balance"]
        except ValueError:
            out["in_base"] = None
        currencies[cur] = out
    jur_out = {jid: {k: round(v, 2) for k, v in b.items()}
               for jid, b in by_jurisdiction.items()}

    return {"base_currency": base, "balance_estimate_base": round(total_base, 2),
            "by_currency": currencies,
            "by_jurisdiction_in_base": jur_out,
            "unconvertible_currencies": sorted(unconvertible),
            "fx_note": "converted with daily-cached rates (seed fallback); "
                       "see amy/jurisdictions/fx_seed.json"}
