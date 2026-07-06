"""Obligations engine (Phase R7A-2) — generic recurring financial duties.

An ObligationRule is pure data loaded from a jurisdiction pack
(rate, wealth_threshold, holding period, calendar_system, schedule,
eligible_account_types) merged with per-user activation config stored in
finance.db. The engine computes CURRENT STATUS (accrued liability, next
due date, progress); the obligation agent turns statuses into briefing
lines, notifications, and payment PROPOSALS through the approval queue —
it never moves or records money on its own.

Hard rules:
  - custodial accounts are excluded from every wealth/income computation,
    regardless of what a pack's eligible_account_types says
  - every status carries the pack's rules (rate, threshold, dates,
    effective version) and disclaimer so the user can verify
  - all figures are estimates, never advice
"""
from __future__ import annotations

import datetime as _dt
import json
import uuid

from ..calendars import get_calendar
from ..jurisdictions import load_pack, obligation_preset

Date = _dt.date


# ---------------------------------------------------------------------------
# Per-user activation storage (finance.db)
# ---------------------------------------------------------------------------

def _ensure_table(fe):
    fe.conn.execute(
        "CREATE TABLE IF NOT EXISTS user_obligations ("
        " id TEXT PRIMARY KEY, jurisdiction TEXT NOT NULL,"
        " preset_id TEXT NOT NULL, status TEXT DEFAULT 'active',"
        " config TEXT DEFAULT '{}', activated_at TEXT,"
        " UNIQUE(jurisdiction, preset_id))")
    fe.conn.commit()


def activate(fe, jurisdiction: str, preset_id: str,
             config: dict | None = None) -> str:
    """Turn a pack preset on for this user. Validates it exists in the pack."""
    pack = load_pack(jurisdiction)
    if obligation_preset(pack, preset_id) is None:
        raise ValueError(f"pack {jurisdiction!r} has no active preset "
                         f"{preset_id!r} for today")
    _ensure_table(fe)
    oid = uuid.uuid4().hex[:12]
    fe.conn.execute(
        "INSERT INTO user_obligations(id,jurisdiction,preset_id,config,activated_at)"
        " VALUES(?,?,?,?,?)"
        " ON CONFLICT(jurisdiction,preset_id) DO UPDATE SET"
        "  status='active', config=excluded.config",
        (oid, jurisdiction.lower(), preset_id, json.dumps(config or {}),
         _dt.datetime.now(_dt.timezone.utc).isoformat()))
    fe.conn.commit()
    row = fe.conn.execute(
        "SELECT id FROM user_obligations WHERE jurisdiction=? AND preset_id=?",
        (jurisdiction.lower(), preset_id)).fetchone()
    return row["id"]


def deactivate(fe, oid: str) -> bool:
    _ensure_table(fe)
    c = fe.conn.execute(
        "UPDATE user_obligations SET status='paused' WHERE id=?", (oid,))
    fe.conn.commit()
    return c.rowcount > 0


def update_config(fe, oid: str, config: dict) -> bool:
    _ensure_table(fe)
    row = fe.conn.execute(
        "SELECT config FROM user_obligations WHERE id=?", (oid,)).fetchone()
    if not row:
        return False
    merged = json.loads(row["config"] or "{}")
    merged.update(config or {})
    fe.conn.execute("UPDATE user_obligations SET config=? WHERE id=?",
                    (json.dumps(merged), oid))
    fe.conn.commit()
    return True


def list_active(fe) -> list[dict]:
    _ensure_table(fe)
    out = []
    for r in fe.conn.execute(
            "SELECT * FROM user_obligations WHERE status='active'").fetchall():
        d = dict(r)
        d["config"] = json.loads(d["config"] or "{}")
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Wealth / income inputs (custodial ALWAYS excluded)
# ---------------------------------------------------------------------------

def _eligible_accounts(fe, eligible_types: list[str] | None) -> list[dict]:
    eligible = set(eligible_types or ["savings", "current", "investment"])
    eligible.discard("custodial")            # hard rail, pack cannot override
    return [a for a in fe.list_accounts()
            if (a.get("account_type") or "savings") in eligible
            and a.get("account_type") != "custodial"]


def qualifying_wealth(fe, eligible_types: list[str] | None) -> float:
    total = 0.0
    for a in _eligible_accounts(fe, eligible_types):
        row = fe.conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM transactions WHERE account_id=?",
            (a["id"],)).fetchone()
        total += float(row["s"] or 0)
    return round(total, 2)


# ---------------------------------------------------------------------------
# Status computation per kind
# ---------------------------------------------------------------------------

def compute_status(fe, ob: dict, on: Date | None = None) -> dict:
    """ob = a row from list_active(). Returns a status dict with the pack
    rules embedded for verification."""
    on = on or _dt.date.today()
    pack = load_pack(ob["jurisdiction"])
    preset = obligation_preset(pack, ob["preset_id"], on)
    if preset is None:
        return {"obligation_id": ob["id"], "preset_id": ob["preset_id"],
                "jurisdiction": ob["jurisdiction"], "state": "no_active_version",
                "disclaimer": pack["disclaimer"]}
    cfg = ob.get("config") or {}
    cal = get_calendar(preset.get("calendar_system", "gregorian"),
                       **(preset.get("calendar_config") or {}))
    kind = preset.get("kind")
    currency = pack["currency"]

    base = {
        "obligation_id": ob["id"], "preset_id": ob["preset_id"],
        "jurisdiction": ob["jurisdiction"], "name": preset["name"],
        "kind": kind, "currency": currency["code"],
        "rules_shown": {
            "rate": preset.get("rate"),
            "wealth_threshold": preset.get("wealth_threshold"),
            "holding_period_years": preset.get("holding_period_years"),
            "calendar_system": preset.get("calendar_system"),
            "schedule": preset.get("schedule"),
            "effective_from": preset.get("effective_from"),
            "effective_to": preset.get("effective_to"),
        },
        "disclaimer": preset["disclaimer"],
    }

    if kind == "wealth_rate":
        wealth = qualifying_wealth(fe, preset.get("eligible_account_types"))
        threshold = (preset.get("wealth_threshold") or {}).get("amount") or 0
        threshold = float(cfg.get("wealth_threshold") or threshold)
        rate = float(cfg.get("rate") or preset.get("rate") or 0)
        above = wealth >= threshold > 0 or (threshold == 0 and wealth > 0)
        anniversary = cfg.get("anniversary_date")
        if anniversary:
            ann = _dt.date.fromisoformat(str(anniversary)[:10])
            while ann <= on:
                ann = cal.add_years(ann, 1)
            next_due = ann
        else:
            period = cal.year_period(on)
            next_due = period.end
        base.update({
            "state": "accruing" if above else "below_threshold",
            "qualifying_wealth": wealth,
            "threshold_used": threshold,
            "estimated_liability": round(rate * wealth, 2) if above else 0.0,
            "next_due": next_due.isoformat(),
            "days_to_due": (next_due - on).days,
            "note": ("Liability accrues only after wealth stays above the "
                     "threshold for the full holding period "
                     f"({preset.get('holding_period_years', 1)} "
                     f"{preset.get('calendar_system')} year)."),
        })
        return base

    if kind == "scheduled_estimate":
        estimate = cfg.get("estimated_annual_amount")
        paid = float(cfg.get("paid_to_date") or 0)
        nxt, nxt_label, nxt_portion = None, None, None
        for item in preset.get("schedule", []):
            if not (item.get("month") and item.get("day")):
                continue
            cand = cal.next_occurrence(item["month"], item["day"], on)
            if nxt is None or cand < nxt:
                nxt, nxt_label = cand, item.get("label")
                nxt_portion = item.get("cumulative_portion")
        if estimate is None:
            base.update({"state": "needs_estimate", "next_due":
                         nxt.isoformat() if nxt else None,
                         "next_label": nxt_label,
                         "note": "Set estimated_annual_amount in the "
                                 "obligation config to get installment figures."})
            return base
        estimate = float(estimate)
        due_cum = round((nxt_portion or 1.0) * estimate, 2)
        base.update({
            "state": "scheduled",
            "estimated_annual_amount": estimate,
            "paid_to_date": paid,
            "next_due": nxt.isoformat() if nxt else None,
            "next_label": nxt_label,
            "amount_due_by_next": max(0.0, round(due_cum - paid, 2)),
            "days_to_due": (nxt - on).days if nxt else None,
        })
        return base

    if kind == "recurring_commitment":
        rate = float(cfg.get("rate") or preset.get("rate") or 0)
        income = 0.0
        try:
            income = float(fe.effective_monthly_income())
        except Exception:
            pass
        base.update({
            "state": "informational",
            "monthly_target": round(rate * income, 2),
            "rate_used": rate,
            "monthly_income_estimate": round(income, 2),
        })
        return base

    if kind == "annual_cap_tracking":
        cap = float((preset.get("wealth_threshold") or {}).get("amount") or 0)
        period = cal.year_period(on)
        contributed = float(cfg.get("contributed_to_date") or 0)
        base.update({
            "state": "tracking",
            "annual_cap": cap,
            "contributed_to_date": contributed,
            "remaining_headroom": round(max(0.0, cap - contributed), 2),
            "period_ends": period.end.isoformat(),
        })
        return base

    base.update({"state": "unsupported_kind"})
    return base


def all_statuses(fe, on: Date | None = None) -> list[dict]:
    return [compute_status(fe, ob, on) for ob in list_active(fe)]
