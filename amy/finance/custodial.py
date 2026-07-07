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


def cycle_commitment(fe: "FinanceEngine", account_id: str) -> dict:
    """What one full cycle costs from this account. Static per-beneficiary
    expected amounts (user-declared) win; median of recent history is the
    fallback. Tracking-only beneficiaries are excluded — their money doesn't
    come from this pool.
    Returns {"total": float, "rows": [{"name", "amount", "source"}]}."""
    from .custodial_ai import beneficiary_history, suggest_amount
    rows = []
    for b in fe.list_beneficiaries(account_id):
        if b.get("tracking_only"):
            continue
        if b.get("split_kind") == "parts" and (b.get("default_parts") or []):
            for p in b["default_parts"]:
                if not isinstance(p, dict) or not p.get("name"):
                    continue
                amt = p.get("amount")
                src = "static"
                if not amt:
                    amt, _ = suggest_amount(
                        beneficiary_history(fe, b["id"], part=p["name"]))
                    src = "median"
                if amt:
                    rows.append({"name": f"{b['name']} · {p['name']}",
                                 "amount": float(amt), "source": src})
            continue
        amt = b.get("expected_amount")
        src = "static"
        if not amt:
            amt, _ = suggest_amount(beneficiary_history(fe, b["id"]))
            src = "median"
        if amt:
            rows.append({"name": b["name"], "amount": float(amt), "source": src})
    return {"total": round(sum(r["amount"] for r in rows), 2), "rows": rows}


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
            "SELECT beneficiary_id, amount, part FROM transactions"
            " WHERE account_id=? AND date=? AND beneficiary_id IS NOT NULL",
            (account_id, latest_cycle_date)).fetchall()
        logged_ids = {r["beneficiary_id"] for r in cycle_txns}
        logged_parts = {(r["beneficiary_id"], r["part"] or "") for r in cycle_txns}

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

        # 2. Skipped-beneficiary check (part-level for split beneficiaries)
        for b in beneficiaries:
            if b["id"] not in logged_ids:
                issues.append({"check": "beneficiary_skipped", "beneficiary": b["name"]})
                continue
            if b.get("split_kind") == "parts":
                for p in (b.get("default_parts") or []):
                    pname = p.get("name") if isinstance(p, dict) else None
                    if pname and (b["id"], pname) not in logged_parts:
                        issues.append({"check": "beneficiary_skipped",
                                       "beneficiary": f"{b['name']} · {pname}"})

    # 3. Low-balance refill check — balance-driven, not date-driven (per user
    # decision): a refill is only flagged when the pool can't cover one full
    # cycle of committed sends (static expected amounts, median fallback).
    # Ad-hoc extras (e.g. a sudden Guru IB send) drain the balance and
    # trigger this naturally.
    commitment = cycle_commitment(fe, account_id)
    balance = fe.custodial_balance(account_id)
    if commitment["total"] > 0 and balance < commitment["total"]:
        issues.append({
            "check": "balance_low_refill",
            "balance": balance,
            "commitment": commitment["total"],
            "shortfall": round(commitment["total"] - balance, 2),
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

    from .custodial_ai import beneficiary_history, suggest_amount

    def _last_txn(bid: str, part: str | None):
        q = ("SELECT id, date, ABS(amount) amt, screenshot_path FROM transactions"
             " WHERE beneficiary_id=? AND amount<0")
        params: list = [bid]
        if part is not None:
            q += " AND COALESCE(part,'')=?"
            params.append(part)
        return fe.conn.execute(q + " ORDER BY date DESC LIMIT 1", params).fetchone()

    beneficiaries = []
    for b in fe.list_beneficiaries(account_id):
        # split beneficiaries (split_kind='parts') get one prefill row per
        # part — e.g. Eswari · Personal and Eswari · MJVR, each with its own
        # history, suggestion, and confirm
        parts = ([p.get("name") for p in (b.get("default_parts") or [])
                  if isinstance(p, dict) and p.get("name")]
                 if b.get("split_kind") == "parts" else [])
        for part in (parts or [None]):
            last = _last_txn(b["id"], part)
            suggested, trend_note = suggest_amount(
                beneficiary_history(fe, b["id"], part=part))
            beneficiaries.append({
                "beneficiary_id": b["id"],
                "name": f"{b['name']} · {part}" if part else b["name"],
                "part": part,
                "sheet_tab": b.get("sheet_tab"),
                "tracking_only": bool(b.get("tracking_only")),
                "last_amount": last["amt"] if last else None,
                "last_date": last["date"] if last else None,
                "suggested_amount": suggested,
                "trend_note": trend_note,
                "transaction_id": last["id"] if last else None,
                "has_screenshot": bool(last and last["screenshot_path"]),
            })
    return {"due_date": due_date, "cadence_days": cadence_days, "beneficiaries": beneficiaries}


def check_low_balance_refill(fe: "FinanceEngine", notification_store,
                             account: dict) -> str | None:
    """Balance-driven refill notification (deduped per day): fires when the
    pool can't cover one full cycle of committed sends. Called from the
    digest loop and after every disbursement, so an ad-hoc extra send that
    drains the balance alerts immediately."""
    aid = account["id"]
    commitment = cycle_commitment(fe, aid)
    balance = fe.custodial_balance(aid)
    if commitment["total"] <= 0 or balance >= commitment["total"]:
        return None
    ref_id = f"custodial_low_balance_{aid}"
    if notification_store.exists_today("custodial_low_balance", ref_id):
        return None
    lines = ", ".join(f"{r['name']} {r['amount']:g}" for r in commitment["rows"])
    return notification_store.create(
        type="custodial_low_balance",
        title=f"Custodial balance low — refill needed ({account.get('nickname', '')})",
        body=(f"Balance {balance:g} can't cover the usual cycle of "
              f"{commitment['total']:g} ({lines}). "
              f"Short by {commitment['total'] - balance:g} — time to ask for a refill."),
        priority="high",
        related_entity={"id": ref_id, "entity_type": "custodial_account",
                        "account_id": aid},
    )


def check_custodial_nudges(fe: "FinanceEngine", notification_store, account: dict) -> str | None:
    """Called from the existing digest loop (amy/saas/app.py _run_all_digests)
    — creates a month-end nudge notification once, deduped per due date.
    Also runs the balance-driven refill check (the only refill alert)."""
    check_low_balance_refill(fe, notification_store, account)
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
