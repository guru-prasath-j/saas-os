"""Custodial account support (e.g. an SBI account held in-trust for someone
else, refilled periodically and forwarded to a fixed set of beneficiaries).

Money in a custodial account is never the user's own — engine.py's
effective_monthly_income()/this_month_spend() already exclude accounts with
account_type == "custodial" so this never pollutes income/budget numbers.
This module only detects refills, checks cycle health, and infers cadence —
it never moves money or initiates a transfer.
"""
from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import FinanceEngine

_DEFAULT_CADENCE_DAYS = 30


def emit_refill_events(fe: "FinanceEngine", events, transactions: list[dict]) -> int:
    """
    Given newly-imported transactions (SyncResult.transactions — each already
    has account_id), emit custodial.refilled for any positive-amount row that
    landed in a custodial account. Returns the count emitted.
    """
    count = 0
    for t in transactions:
        aid = t.get("account_id")
        if not aid or t.get("amount", 0) <= 0:
            continue
        acc = fe.get_account(aid)
        if not acc or acc.get("account_type") != "custodial":
            continue
        events.emit("custodial.refilled", {
            "account_id": aid,
            "transaction_id": t.get("id"),
            "amount": t["amount"],
            "date": t.get("date"),
            "merchant": t.get("merchant", ""),
        }, source="gmail_sync")
        count += 1
    return count


def _infer_cadence(cycle_dates: list[str]) -> int:
    """Median gap (days) between past disbursement dates. Needs >=2 dates;
    otherwise falls back to a 30-day default until real history exists."""
    if len(cycle_dates) < 2:
        return _DEFAULT_CADENCE_DAYS
    dates = sorted((_dt.date.fromisoformat(d) for d in cycle_dates), reverse=True)
    gaps = sorted((dates[i] - dates[i + 1]).days for i in range(len(dates) - 1))
    mid = len(gaps) // 2
    median = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2
    return max(1, round(median))


def run_validation(fe: "FinanceEngine", account_id: str) -> dict:
    """
    Three checks against the user's own custodial-account data (no second
    role/persona — just flags surfaced to the same single user):
      1. Split-sum check (e.g. Eswari's two parts add up to the expected total)
      2. Skipped-beneficiary check (did everyone usually paid get logged?)
      3. Overdue-refill check (is a refill late relative to inferred cadence?)
    """
    issues: list[dict] = []
    beneficiaries = fe.list_beneficiaries(account_id)
    cycle_dates = fe.custodial_cycle_dates(account_id)
    latest_cycle_date = cycle_dates[0] if cycle_dates else None

    if latest_cycle_date:
        cycle_txns = fe.conn.execute(
            "SELECT beneficiary_id, amount FROM transactions"
            " WHERE account_id=? AND date=? AND beneficiary_id IS NOT NULL",
            (account_id, latest_cycle_date)).fetchall()
        logged_ids = {r["beneficiary_id"] for r in cycle_txns}

        # 1. Split-sum check — Eswari-prefixed beneficiaries this cycle vs
        # the expected total configured on the account (accounts.meta).
        eswari_ids = {b["id"] for b in beneficiaries if b["name"].startswith("Eswari")}
        if eswari_ids:
            total = sum(abs(r["amount"]) for r in cycle_txns if r["beneficiary_id"] in eswari_ids)
            acc = fe.get_account(account_id) or {}
            expected = (acc.get("meta") or {}).get("eswari_expected_total")
            if expected and abs(total - expected) > 0.01:
                issues.append({
                    "check": "split_sum_mismatch", "beneficiary": "Eswari",
                    "expected": expected, "actual": round(total, 2),
                })

        # 2. Skipped-beneficiary check
        for b in beneficiaries:
            if b["id"] not in logged_ids:
                issues.append({"check": "beneficiary_skipped", "beneficiary": b["name"]})

    # 3. Overdue-refill check
    cadence_days = _infer_cadence(cycle_dates)
    if latest_cycle_date:
        due = _dt.date.fromisoformat(latest_cycle_date) + _dt.timedelta(days=cadence_days)
        if _dt.date.today() >= due:
            last_refill = fe.conn.execute(
                "SELECT MAX(date) d FROM transactions WHERE account_id=? AND amount>0",
                (account_id,)).fetchone()["d"]
            if not last_refill or last_refill < due.isoformat():
                issues.append({
                    "check": "refill_overdue", "due_date": due.isoformat(),
                    "last_refill": last_refill,
                })

    return {"issues": issues, "checked_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}


def next_cycle_prefill(fe: "FinanceEngine", account_id: str) -> dict:
    """Due date + each beneficiary's last logged amount — the editable
    starting point the UI shows for the month-end nudge."""
    cycle_dates = fe.custodial_cycle_dates(account_id)
    cadence_days = _infer_cadence(cycle_dates)
    due_date = None
    if cycle_dates:
        due_date = (_dt.date.fromisoformat(cycle_dates[0])
                    + _dt.timedelta(days=cadence_days)).isoformat()

    last_cycle = {r["beneficiary_id"]: r for r in fe.custodial_last_cycle(account_id)}
    beneficiaries = []
    for b in fe.list_beneficiaries(account_id):
        last = last_cycle.get(b["id"])
        beneficiaries.append({
            "beneficiary_id": b["id"],
            "name": b["name"],
            "sheet_tab": b.get("sheet_tab"),
            "last_amount": abs(last["amount"]) if last else None,
            "last_date": last["date"] if last else None,
            "transaction_id": last["id"] if last else None,
            "has_screenshot": bool(last and last.get("screenshot_path")),
        })
    return {"due_date": due_date, "cadence_days": cadence_days, "beneficiaries": beneficiaries}


def check_custodial_nudges(fe: "FinanceEngine", notification_store, account: dict) -> str | None:
    """Called from the existing digest loop (amy/saas/app.py _run_all_digests)
    — creates a month-end nudge notification once, deduped per due date."""
    aid = account["id"]
    cycle_dates = fe.custodial_cycle_dates(aid)
    if not cycle_dates:
        return None
    cadence_days = _infer_cadence(cycle_dates)
    last = _dt.date.fromisoformat(cycle_dates[0])
    due = last + _dt.timedelta(days=cadence_days)
    if _dt.date.today() < due:
        return None

    ref_id = f"custodial_nudge_{aid}_{due.isoformat()}"
    if notification_store.exists_today("custodial_nudge", ref_id):
        return None

    last_cycle = fe.custodial_last_cycle(aid)
    names = sorted({
        b["name"] for b in fe.list_beneficiaries(aid)
        if b["id"] in {r["beneficiary_id"] for r in last_cycle}
    })
    return notification_store.create(
        type="custodial_nudge",
        title="Month-end custodial disbursement due",
        body=f"Last cycle was {last.isoformat()}. Usually disbursed: " + ", ".join(names),
        priority="normal",
        related_entity={"id": ref_id, "entity_type": "custodial_account", "account_id": aid},
    )
