"""LIFE AUTOPILOT L2 — historical backfill.

backfill(ctx, start_date, end_date) recomputes life_metrics for a date
range using only signals actually recorded historically — never retro-
infers a day_type/metric from data that wasn't there (hard rule 7). Early
days in this account's history have transactions but no geo history at
all, so geo-derived columns (office_minutes, commute, gym_visits,
home_arrival_at, sleep_window) will legitimately be NULL for those days
while finance-derived columns (meals_out, cafe_spend) are populated —
that's the honest reading of what's actually on file, not a bug.

CLI: python -m amy.life.backfill <email> <start-date> <end-date>
Looks the user up by email in amy_saas.db rather than taking a uid
directly (CLAUDE.md: never hardcode a uid, look it up — uids go stale).
"""
from __future__ import annotations

import datetime as _dt


def backfill(ctx, start_date: str, end_date: str) -> dict:
    from .aggregator import compute_day

    d = _dt.date.fromisoformat(start_date)
    end = _dt.date.fromisoformat(end_date)
    if d > end:
        raise ValueError("start_date must be <= end_date")
    computed = []
    while d <= end:
        date_s = d.isoformat()
        row = compute_day(ctx, date_s)
        ctx.store.upsert_life_metrics(ctx.user_id, date_s, **row)
        computed.append({"date": date_s, "day_type": row["day_type"]})
        d += _dt.timedelta(days=1)
    return {"days_computed": len(computed), "days": computed}


def _main() -> None:
    import sys

    from ..collab import CollabDB
    from ..saas import paths
    from ..saas.db import SessionLocal, User
    from .. import config
    from ..automation.jobs import build_ctx

    if len(sys.argv) != 4:
        print("usage: python -m amy.life.backfill <email> <start-date YYYY-MM-DD> <end-date YYYY-MM-DD>")
        sys.exit(1)
    email, start_date, end_date = sys.argv[1], sys.argv[2], sys.argv[3]

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
    finally:
        db.close()
    if not user:
        print(f"no user with email {email!r}")
        sys.exit(1)

    index_dir = paths.index_dir(user.id)
    cdb = CollabDB(str(index_dir / "collab.db"))
    try:
        ctx = build_ctx(user.id, user.email, cdb, index_dir, llm_router=None)
        result = backfill(ctx, start_date, end_date)
        print(f"backfilled {result['days_computed']} days for {email}")
    finally:
        cdb.close()


if __name__ == "__main__":
    _main()
