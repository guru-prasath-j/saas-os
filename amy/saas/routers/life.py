"""Life Autopilot routes (/api/life) — LIFE AUTOPILOT L2.

GET /api/life/metrics is read-only over the life_metrics table computed by
the life_metrics_daily job / backfill — no side effects, so L2 is
independently testable via the API ahead of L7's UI landing.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..db import User
from ..deps import current_user
from .automation import _ctx

router = APIRouter()


@router.get("/api/life/metrics")
def list_life_metrics(from_: str = Query("", alias="from"), to: str = "",
                      user: User = Depends(current_user)):
    import datetime as _dt

    since = from_ or (_dt.date.today() - _dt.timedelta(days=30)).isoformat()
    until = to or _dt.date.today().isoformat()
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        rows = ctx.store.list_life_metrics(user.id, since, until)
        return {"metrics": rows, "from": since, "to": until}
    finally:
        cdb.close()
