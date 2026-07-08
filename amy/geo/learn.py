"""Place learning (CONTEXT_PLAN C2) — learn geofences from spending patterns.

The user never has to draw a geofence: if a recurring merchant's transaction
days keep coinciding with the same unmatched ~110 m grid cell in the location
history, that cell almost certainly *is* the merchant. The correlator proposes
it as a place through the Approval Inbox (tier 2 — creating a geofence is a
write), and on approval the errand/spend agents start working there.

Day-level correlation only: transactions carry dates, not times, and geo_cells
deliberately stores nothing finer. Coordinates never reach an LLM — this is
pure set arithmetic.
"""
from __future__ import annotations

import datetime as _dt

from .store import GeoStore, cell_center, haversine_m

MIN_TXN_DAYS = 3          # merchant must recur before it's worth a geofence
MIN_OVERLAP_DAYS = 2      # cell and merchant must coincide at least this often
MIN_SCORE = 0.6           # overlap / smaller-of-the-two-day-sets
LOOKBACK_DAYS = 60        # matches geo_cells retention
NEAR_EXISTING_M = 250     # cell this close to a saved place is that place

# merchant category (finance categorizer output) → place kind the errand and
# budget agents understand; anything unmapped falls back to the category itself
_CATEGORY_KIND = {
    "food": "grocery",
    "groceries": "grocery",
    "dining": "restaurant",
    "shopping": "shopping",
    "transport": "fuel",
    "health": "pharmacy",
}


def suggest_places(fe, gs: GeoStore) -> list[dict]:
    """Correlate recurring merchants with recurring unmatched cells."""
    since = (_dt.date.today() - _dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    txns = fe.list_transactions(limit=2000, since=since)

    merchant_days: dict[str, set[str]] = {}
    merchant_cats: dict[str, dict[str, int]] = {}
    for t in txns:
        m = (t.get("merchant") or "").strip()
        if not m or (t.get("amount") or 0) >= 0:
            continue   # debits only: you are physically at places you pay
        merchant_days.setdefault(m, set()).add(t["date"][:10])
        cats = merchant_cats.setdefault(m, {})
        cat = (t.get("category") or "").strip()
        if cat and cat.lower() != "uncategorized":
            cats[cat] = cats.get(cat, 0) + 1

    cells = gs.cell_days(min_days=MIN_OVERLAP_DAYS)
    places = gs.list_places()
    suggestions: list[dict] = []

    for merchant, mdays in merchant_days.items():
        if len(mdays) < MIN_TXN_DAYS:
            continue
        best = None
        for cell, cdays in cells.items():
            overlap = mdays & cdays
            if len(overlap) < MIN_OVERLAP_DAYS:
                continue
            score = len(overlap) / min(len(mdays), len(cdays))
            if score < MIN_SCORE:
                continue
            if best is None or len(overlap) > len(best[1]):
                best = (cell, overlap, score)
        if best is None:
            continue
        cell, overlap, score = best
        lat, lon = cell_center(cell)
        if any(haversine_m(lat, lon, p["lat"], p["lon"])
               <= max(NEAR_EXISTING_M, p["radius_m"] or 0)
               for p in places):
            continue   # that spot is already a saved place
        cats = merchant_cats.get(merchant) or {}
        top_cat = max(cats, key=cats.get) if cats else ""
        kind = _CATEGORY_KIND.get(top_cat.lower(), top_cat.lower())
        suggestions.append({
            "merchant": merchant, "lat": lat, "lon": lon, "cell": cell,
            "kind": kind, "category": top_cat,
            "overlap_days": sorted(overlap), "score": round(score, 2),
        })
    return suggestions


def place_learning(ctx) -> dict:
    """Job handler: propose learned places as tier-2 approvals (deduped)."""
    from ..automation.executors import submit_action
    gs = GeoStore(ctx.collab)
    fe = ctx.open_finance()
    try:
        suggestions = suggest_places(fe, gs)
    finally:
        fe.close()
    proposed, duplicates = 0, 0
    for s in suggestions:
        reasoning = (
            f"'{s['merchant']}' charges and your presence in the same ~110 m "
            f"area coincided on {len(s['overlap_days'])} days "
            f"({', '.join(s['overlap_days'][-3:])}…, match score {s['score']}). "
            "Saving it as a place enables errand reminders and pre-purchase "
            "budget warnings there. Coordinates were correlated locally; "
            "no LLM involved.")
        out = submit_action(
            ctx, tier=2, action_type="add_place",
            title=f"Save learned place: {s['merchant']}",
            body=reasoning,
            payload={"name": s["merchant"], "lat": s["lat"], "lon": s["lon"],
                     "kind": s["kind"], "radius_m": 150, "source": "learned"},
            source="place_learning", reasoning=reasoning, risk="write",
            affected_entity=f"place={s['merchant']}",
            dedup_key=f"add_place_{s['cell']}_{s['merchant'][:40]}")
        if out.get("status") == "duplicate":
            duplicates += 1
        else:
            proposed += 1
    return {"suggestions": len(suggestions), "proposed": proposed,
            "duplicates": duplicates}
