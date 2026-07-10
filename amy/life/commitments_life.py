"""LIFE AUTOPILOT L8 — commitments crossover.

pharmacy_refill_check: a regular pharmacy-merchant transaction cadence
(patterns.merchant_cadences) proposes a tier-2 'refill' commitment
(kind='custom', titled 'Refill: {merchant}') — this is exactly what L9's
pharmacy rule was waiting for (it looks for an open commitment titled
'refill'; nothing created one until this part).

annual_checkup_check: proposes ONE annual health-checkup commitment per
calendar year if none exists yet for the current year. Both reuse L3's
propose() framework (dedup/resuggest-window/drift-silence — the anti-nag
needs are identical) and the existing commitments tier-2 path
(add_commitment executor -> CommitmentEngine.add()) — the deadline ladder
(DUE_SOON/DUE_UPCOMING rungs) is CommitmentEngine's own, unchanged.
"""
from __future__ import annotations

import datetime as _dt

_PHARMACY_TOKENS = ("pharmacy", "pharmac", "apollo", "medplus", "1mg",
                    "netmeds", "pharmeasy", "wellness forever")


def pharmacy_refill_check(ctx) -> list[dict]:
    from .inference import propose

    fe = ctx.open_finance()
    try:
        from ..patterns import merchant_cadences
        from ..commitments import CommitmentEngine
        cadences = merchant_cadences(fe)
        existing_titles = {c["title"].lower() for c in CommitmentEngine(fe).list("open")}
    finally:
        fe.close()

    out = []
    for c in cadences:
        merchant = c["merchant"]
        if not any(tok in merchant.lower() for tok in _PHARMACY_TOKENS):
            continue
        title = f"Refill: {merchant}"
        if title.lower() in existing_titles:
            continue
        due = (_dt.date.fromisoformat(c["last_date"])
              + _dt.timedelta(days=c["gap_days"])).isoformat()
        result = propose(
            ctx, "commitments_crossover", f"refill_{merchant[:30]}",
            title=f"Track refill: {merchant}?",
            body=f"Regular pharmacy purchase cadence every {c['gap_days']} days at {merchant}.",
            action_type="add_commitment",
            payload={"kind": "custom", "title": title, "due_date": due,
                    "merchant": merchant, "source": "agent"},
            reasoning=f"Pharmacy cadence every {c['gap_days']} days, {c['occurrences']} occurrences.")
        if result:
            out.append(result)
    return out


def annual_checkup_check(ctx) -> list[dict]:
    from .inference import propose

    fe = ctx.open_finance()
    try:
        from ..commitments import CommitmentEngine
        existing = CommitmentEngine(fe).list("all", limit=500)
    finally:
        fe.close()

    year = _dt.date.today().year
    already = any("health checkup" in (c.get("title") or "").lower()
                 and (c.get("due_date") or "")[:4] == str(year) for c in existing)
    if already:
        return []
    due = _dt.date(year, 12, 31).isoformat()
    result = propose(
        ctx, "commitments_crossover", f"checkup_{year}",
        title="Schedule your annual health checkup?",
        body=f"No annual health-checkup commitment found for {year}.",
        action_type="add_commitment",
        payload={"kind": "custom", "title": f"Annual health checkup {year}",
                "due_date": due, "source": "agent"},
        reasoning=f"No {year} health-checkup commitment on file.")
    return [result] if result else []
