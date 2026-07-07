"""Finance CFO routes — transactions, budgets, subscriptions, investments, income, afford,
accounts, and CSV import."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from ..db import User
from .. import paths, tenancy
from ..deps import current_user, _collab_db_path

router = APIRouter()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _finance_db(user: "User"):
    from ...finance import FinanceEngine
    return FinanceEngine(str(paths.index_dir(user.id) / "finance.db"))


def _open_collab(user: "User"):
    from ...collab import CollabDB
    return CollabDB(_collab_db_path(user))


def _emit_fin(user: "User", event_type: str, payload: dict) -> None:
    """Fire-and-forget finance event. Never raises — bad event must not break the route.

    Reactive agents (amy/agents/reactive.py) are wired onto the store before
    emitting so they react synchronously to route-driven imports too; wiring
    failures degrade to a plain emit."""
    try:
        from ...events.store import EventStore
        cdb = _open_collab(user)
        try:
            es = EventStore(cdb)
            try:
                from ...agents.reactive import register_reactive_agents
                from ...automation.jobs import build_ctx
                ctx = build_ctx(user.id, user.email, cdb,
                                paths.index_dir(user.id), llm_router=None)
                register_reactive_agents(es, ctx)
            except Exception:
                pass   # agents are optional; the event itself must still emit
            es.emit(event_type, payload, source="finance")
        finally:
            cdb.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class TransactionBody(BaseModel):
    amount: float
    category: str = "Uncategorized"
    merchant: str = ""
    date: str | None = None
    source: str = "manual"
    notes: str = ""


class BudgetBody(BaseModel):
    category: str
    monthly_limit: float


class SubscriptionBody(BaseModel):
    name: str
    monthly_cost: float = 0
    annual_cost: float = 0
    renewal_date: str | None = None
    auto_renew: bool = True
    payment_method: str = ""
    status: str = "active"


class SubscriptionPatch(BaseModel):
    name: str | None = None
    monthly_cost: float | None = None
    annual_cost: float | None = None
    renewal_date: str | None = None
    auto_renew: bool | None = None
    payment_method: str | None = None
    status: str | None = None


class InvestmentBody(BaseModel):
    type: str
    name: str
    current_value: float = 0
    cost_basis: float = 0


class InvestmentPatch(BaseModel):
    current_value: float | None = None
    cost_basis: float | None = None


class IncomeBody(BaseModel):
    name: str
    type: str = "salary"
    amount: float
    recurrence: str = "monthly"


class AffordBody(BaseModel):
    amount: float
    description: str = ""
    # optional financing comparison (R7A-4): when months is set, the response
    # compares total cost across the financing models enabled by the user's
    # jurisdiction pack (values profiles may flag models)
    financing_months: int | None = None
    financing_annual_rate: float = 0.12


class AccountBody(BaseModel):
    nickname: str
    bank_name: str
    account_type: str = "savings"
    sync_method: str = "manual"
    meta: dict = {}


class AccountPatch(BaseModel):
    nickname: str | None = None
    bank_name: str | None = None
    account_type: str | None = None
    sync_method: str | None = None
    meta: dict | None = None


class ColumnMapBody(BaseModel):
    column_map: dict


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

@router.get("/api/finance/overview")
def finance_overview(user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        return fe.overview()
    finally:
        fe.close()


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

@router.post("/api/finance/transactions")
def add_transaction(body: TransactionBody, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        tid = fe.add_transaction(
            body.amount, body.category, body.merchant,
            body.date, body.source, body.notes)
        _emit_fin(user, "finance.transaction_added", {
            "id": tid, "amount": body.amount, "category": body.category,
            "merchant": body.merchant, "source": body.source,
        })
        return {"id": tid}
    finally:
        fe.close()


@router.get("/api/finance/transactions")
def list_transactions(limit: int = 500, category: str | None = None,
                      since: str | None = None, until: str | None = None,
                      account_id: str | None = None,
                      user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        return {"transactions": fe.list_transactions(limit, category, since, until, account_id)}
    finally:
        fe.close()


@router.get("/api/finance/duplicates")
def get_duplicates(date_window: int = 1, user: User = Depends(current_user)):
    """Scan transactions and return groups of potential duplicates."""
    from ...finance.dedup import find_duplicates
    fe = _finance_db(user)
    try:
        groups = find_duplicates(fe.conn, date_window=date_window)
        total_extra = sum(g["count"] - 1 for g in groups)
        return {
            "groups": groups,
            "total_groups": len(groups),
            "total_duplicates": total_extra,
        }
    finally:
        fe.close()


class _ResolveBody(BaseModel):
    delete_ids: list[str]


@router.post("/api/finance/duplicates/resolve")
def resolve_duplicates(body: _ResolveBody, user: User = Depends(current_user)):
    """Delete the specified transaction IDs (user chose which copies to remove)."""
    if not body.delete_ids:
        return {"deleted": 0}
    fe = _finance_db(user)
    try:
        placeholders = ",".join("?" * len(body.delete_ids))
        count = fe.conn.execute(
            f"DELETE FROM transactions WHERE id IN ({placeholders})", body.delete_ids
        ).rowcount
        fe.conn.commit()
        return {"deleted": count}
    finally:
        fe.close()


@router.delete("/api/finance/duplicates/auto")
def auto_remove_duplicates(user: User = Depends(current_user)):
    """Auto-delete exact duplicates — keeps the oldest import of each group."""
    from ...finance.dedup import auto_resolve_exact
    fe = _finance_db(user)
    try:
        deleted = auto_resolve_exact(fe.conn)
        return {"deleted": deleted}
    finally:
        fe.close()


@router.delete("/api/finance/transactions")
def reset_all_transactions(confirm: str = "", user: User = Depends(current_user)):
    """Delete ALL transactions for this user (irreversible reset).

    Destructive full-wipe: requires the explicit confirmation token
    ?confirm=DELETE-ALL-TRANSACTIONS so no single unqualified call (from the
    UI, an agent, or a mistyped script) can erase the ledger."""
    if confirm != "DELETE-ALL-TRANSACTIONS":
        raise HTTPException(
            status_code=400,
            detail="Full wipe requires ?confirm=DELETE-ALL-TRANSACTIONS")
    fe = _finance_db(user)
    try:
        count = fe.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        fe.conn.execute("DELETE FROM transactions")
        fe.conn.commit()
        _emit_fin(user, "finance.transactions_reset", {"deleted": count})
        return {"deleted": count}
    finally:
        fe.close()


@router.delete("/api/finance/transactions/{tid}")
def delete_transaction(tid: str, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        if not fe.delete_transaction(tid):
            raise HTTPException(status_code=404, detail="transaction not found")
        return {"ok": True}
    finally:
        fe.close()


@router.post("/api/finance/transactions/auto-categorize")
def auto_categorize(user: User = Depends(current_user)):
    """
    Run rule-based categorization on all Uncategorized transactions (also
    re-checks 'Income'-tagged transactions on credit-card accounts, since a
    credit there is a bill payment, not income). Whatever rules can't resolve
    gets one batched LLM call.
    """
    from ...finance.categorizer import auto_categorize_all
    from ...llm import LLMRouter
    from ..deps import _user_key
    fe = _finance_db(user)
    try:
        llm = LLMRouter(openai_api_key=_user_key(user), use_global_keys=True)
        updated = auto_categorize_all(fe, llm=llm)
        return {"updated": updated}
    finally:
        fe.close()


@router.patch("/api/finance/transactions/{tid}/category")
def set_category(tid: str, body: dict, user: User = Depends(current_user)):
    """Manually set category for a single transaction. The correction is also
    saved as a learned rule so future imports categorize this merchant right."""
    cat = body.get("category", "Uncategorized")
    fe = _finance_db(user)
    try:
        row = fe.conn.execute(
            "SELECT merchant FROM transactions WHERE id=?", (tid,)).fetchone()
        fe.conn.execute("UPDATE transactions SET category=? WHERE id=?", (cat, tid))
        fe.conn.commit()
        learned = None
        if row and row["merchant"]:
            try:
                from ...automation.learning import learn_from_correction
                learned = learn_from_correction(fe, row["merchant"], cat)
            except Exception:
                pass
        return {"ok": True, "learned_pattern": learned}
    finally:
        fe.close()


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------

@router.post("/api/finance/budgets")
def set_budget(body: BudgetBody, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        fe.set_budget(body.category, body.monthly_limit)
        _emit_fin(user, "finance.budget_set", {
            "category": body.category,
            "monthly_limit": body.monthly_limit,
        })
        return {"ok": True, "category": body.category, "monthly_limit": body.monthly_limit}
    finally:
        fe.close()


@router.get("/api/finance/budgets")
def list_budgets(user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        return {"budgets": fe.list_budgets(), "status": fe.budget_status()}
    finally:
        fe.close()


@router.post("/api/finance/budgets/suggestions")
def suggest_budgets(user: User = Depends(current_user)):
    """
    Propose monthly budget caps per category from income + this month's spend
    + the user's profile location (cost-of-living context). Re-runs fresh on
    every call — no persistence; accept or edit each suggestion in the UI.
    """
    from ...finance.budget_suggest import suggest_budgets as _suggest
    from ...llm import LLMRouter
    from ..deps import _user_key
    fe = _finance_db(user)
    try:
        llm = LLMRouter(openai_api_key=_user_key(user), use_global_keys=True)
        return _suggest(fe, user.location, llm)
    finally:
        fe.close()


@router.delete("/api/finance/budgets/{category}")
def delete_budget(category: str, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        if not fe.delete_budget(category):
            raise HTTPException(status_code=404, detail="budget not found")
        return {"ok": True}
    finally:
        fe.close()


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

@router.post("/api/finance/subscriptions")
def add_subscription(body: SubscriptionBody, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        sid = fe.add_subscription(
            body.name, body.monthly_cost, body.annual_cost,
            body.renewal_date, body.auto_renew, body.payment_method, body.status)
        _emit_fin(user, "finance.subscription_added", {
            "id": sid,
            "name": body.name,
            "monthly_cost": body.monthly_cost,
        })
        return {"id": sid}
    finally:
        fe.close()


@router.get("/api/finance/subscriptions")
def list_subscriptions(status: str | None = "active", user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        return {
            "subscriptions": fe.list_subscriptions(status),
            "monthly_total": fe.subscription_total_monthly(),
        }
    finally:
        fe.close()


@router.get("/api/finance/subscriptions/insights")
def subscription_insights(user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        return fe.subscription_insights()
    finally:
        fe.close()


@router.post("/api/finance/subscriptions/suggestions")
def suggest_subscriptions(user: User = Depends(current_user)):
    """
    Scan transaction history for likely recurring charges not yet tracked as
    subscriptions. Re-runs fresh on every call (no persistence) so the review
    list always reflects current transaction data — accept or dismiss each
    suggestion from the UI.
    """
    from ...finance.subscription_detect import detect_subscriptions
    from ...llm import LLMRouter
    from ..deps import _user_key
    fe = _finance_db(user)
    try:
        llm = LLMRouter(openai_api_key=_user_key(user), use_global_keys=True)
        return {"suggestions": detect_subscriptions(fe, llm)}
    finally:
        fe.close()


@router.patch("/api/finance/subscriptions/{sid}")
def update_subscription(sid: str, body: SubscriptionPatch,
                        user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        updates = {k: v for k, v in body.model_dump().items() if v is not None}
        if not fe.update_subscription(sid, **updates):
            raise HTTPException(status_code=404, detail="subscription not found")
        return {"ok": True}
    finally:
        fe.close()


@router.delete("/api/finance/subscriptions/{sid}")
def delete_subscription(sid: str, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        if not fe.delete_subscription(sid):
            raise HTTPException(status_code=404, detail="subscription not found")
        return {"ok": True}
    finally:
        fe.close()


# ---------------------------------------------------------------------------
# Investments
# ---------------------------------------------------------------------------

@router.post("/api/finance/investments")
def add_investment(body: InvestmentBody, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        iid = fe.add_investment(body.type, body.name, body.current_value, body.cost_basis)
        _emit_fin(user, "finance.investment_added", {
            "id": iid,
            "name": body.name,
            "type": body.type,
            "current_value": body.current_value,
        })
        return {"id": iid}
    finally:
        fe.close()


@router.get("/api/finance/investments")
def list_investments(user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        return {
            "investments": fe.list_investments(),
            "portfolio": fe.portfolio_summary(),
        }
    finally:
        fe.close()


@router.patch("/api/finance/investments/{iid}")
def update_investment(iid: str, body: InvestmentPatch,
                      user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        if not fe.update_investment(iid, body.current_value, body.cost_basis):
            raise HTTPException(status_code=404, detail="investment not found")
        return {"ok": True}
    finally:
        fe.close()


@router.delete("/api/finance/investments/{iid}")
def delete_investment(iid: str, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        if not fe.delete_investment(iid):
            raise HTTPException(status_code=404, detail="investment not found")
        return {"ok": True}
    finally:
        fe.close()


# ---------------------------------------------------------------------------
# Income sources
# ---------------------------------------------------------------------------

@router.post("/api/finance/income")
def add_income(body: IncomeBody, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        sid = fe.add_income_source(body.name, body.type, body.amount, body.recurrence)
        _emit_fin(user, "finance.income_added", {
            "id": sid,
            "name": body.name,
            "type": body.type,
            "amount": body.amount,
        })
        return {"id": sid}
    finally:
        fe.close()


@router.get("/api/finance/income")
def list_income(user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        return {
            "income_sources": fe.list_income_sources(),
            "monthly_total": round(fe.monthly_income(), 2),
        }
    finally:
        fe.close()


@router.delete("/api/finance/income/{sid}")
def delete_income(sid: str, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        if not fe.delete_income_source(sid):
            raise HTTPException(status_code=404, detail="income source not found")
        return {"ok": True}
    finally:
        fe.close()


# ---------------------------------------------------------------------------
# Afford engine
# ---------------------------------------------------------------------------

@router.post("/api/finance/afford")
def can_afford(body: AffordBody, user: User = Depends(current_user)):
    from ...finance.afford import can_afford as _can_afford
    fe = _finance_db(user)
    cdb = _open_collab(user)
    try:
        result = _can_afford(body.amount, body.description, fe, collab_db=cdb)
        if body.financing_months and body.financing_months > 0:
            try:
                from ...financing import compare, flagged_models_from_profiles
                from ...values import list_profiles
                from .jurisdictions import home_pack
                pack = home_pack(user)
                result["financing_options"] = compare(
                    body.amount, body.financing_months,
                    body.financing_annual_rate,
                    enabled_models=pack.get("financing_models"),
                    flagged_models=flagged_models_from_profiles(
                        list_profiles(fe, enabled_only=True)))
                result["financing_note"] = (
                    f"Models enabled by the '{pack['id']}' jurisdiction pack; "
                    "totals assume the given rate/markup. Estimates only.")
            except Exception:
                pass   # financing comparison is additive; never break afford
        return result
    finally:
        fe.close()
        cdb.close()


# ---------------------------------------------------------------------------
# Financial goals (via existing CollabDB goals table, domain="finance")
# ---------------------------------------------------------------------------

@router.get("/api/finance/goals")
def finance_goals(user: User = Depends(current_user)):
    cdb = _open_collab(user)
    try:
        rows = cdb.conn.execute(
            "SELECT id, title, status, progress, created_at, target_date"
            " FROM goals WHERE domain='finance' ORDER BY created_at DESC"
        ).fetchall()
        goals = [dict(r) for r in rows]
        # Add monthly savings required for each active goal with a target date
        import datetime as _dt
        fe = _finance_db(user)
        try:
            monthly_income = fe.monthly_income()
            for g in goals:
                g["monthly_savings_required"] = None
                if (g["status"] == "active" and g["target_date"]
                        and g["progress"] is not None and g["progress"] < 1.0):
                    try:
                        target = _dt.date.fromisoformat(g["target_date"])
                        months_left = max(1, (target - _dt.date.today()).days / 30)
                        remaining_fraction = 1.0 - g["progress"]
                        if monthly_income > 0:
                            # express as % of monthly income needed
                            g["monthly_savings_required"] = round(
                                remaining_fraction * monthly_income / months_left, 2)
                    except Exception:
                        pass
        finally:
            fe.close()
        return {"goals": goals}
    finally:
        cdb.close()


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

@router.post("/api/finance/accounts")
def create_account(body: AccountBody, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        aid = fe.add_account(
            body.nickname, body.bank_name, body.account_type,
            body.sync_method, body.meta or None)
        return {"id": aid}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        fe.close()


@router.get("/api/finance/accounts")
def list_accounts(user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        return {"accounts": fe.list_accounts()}
    finally:
        fe.close()


@router.get("/api/finance/accounts/{aid}")
def get_account(aid: str, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        acc = fe.get_account(aid)
        if acc is None:
            raise HTTPException(status_code=404, detail="account not found")
        return acc
    finally:
        fe.close()


@router.patch("/api/finance/accounts/{aid}")
def update_account(aid: str, body: AccountPatch, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        updates = {k: v for k, v in body.model_dump().items() if v is not None}
        if not updates:
            raise HTTPException(status_code=422, detail="no fields to update")
        if not fe.update_account(aid, **updates):
            raise HTTPException(status_code=404, detail="account not found")
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        fe.close()


@router.delete("/api/finance/accounts/{aid}")
def delete_account(aid: str, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        if not fe.delete_account(aid):
            raise HTTPException(status_code=404, detail="account not found")
        return {"ok": True}
    finally:
        fe.close()


@router.get("/api/finance/accounts/{aid}/transactions")
def account_transactions(aid: str, limit: int = 100,
                         since: str | None = None, until: str | None = None,
                         user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        if fe.get_account(aid) is None:
            raise HTTPException(status_code=404, detail="account not found")
        txns = fe.list_transactions(limit=limit, since=since, until=until,
                                     account_id=aid)
        return {"transactions": txns, "count": len(txns)}
    finally:
        fe.close()


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------

@router.post("/api/finance/accounts/{aid}/preview/csv")
async def preview_csv_upload(
    aid: str,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
):
    """Parse CSV and return extracted transactions WITHOUT saving to DB."""
    from ...finance.sync.csv_import import (
        _xls_to_csv, preview_csv, _auto_detect_columns,
        parse_csv_preview_only,
    )
    from ...finance.sync.bank_presets import detect_preset

    fe = _finance_db(user)
    try:
        account = fe.get_account(aid)
        if account is None:
            raise HTTPException(status_code=404, detail="account not found")
        raw = await file.read()
        filename = file.filename or ""
        ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
        if ext in ("xls", "xlsx") or raw[:2] in (b"PK", b"\xd0\xcf"):
            raw = _xls_to_csv(raw, filename)
        pv = preview_csv(raw)
        column_map = fe.get_column_map(account["bank_name"])
        if column_map is None:
            preset = detect_preset(pv["headers"])
            column_map = preset.column_map if preset else _auto_detect_columns(pv["headers"], pv["sample_rows"])
        if column_map is None:
            return {"needs_mapping": True, "headers": pv["headers"], "sample_rows": pv["sample_rows"]}
        txns = parse_csv_preview_only(raw, column_map, filename,
                                      account_type=account.get("account_type", ""))
        return {"transactions": txns, "count": len(txns)}
    finally:
        fe.close()


@router.post("/api/finance/accounts/{aid}/preview/pdf")
async def preview_pdf_upload(
    aid: str,
    file: UploadFile = File(...),
    password: str | None = None,
    user: User = Depends(current_user),
):
    """Parse PDF and return extracted transactions WITHOUT saving to DB."""
    from ...finance.sync.pdf_import import parse_pdf_preview_only, PasswordRequired
    from ...llm import LLMRouter
    from ..deps import _user_key

    fe = _finance_db(user)
    try:
        acc = fe.get_account(aid)
        if acc is None:
            raise HTTPException(status_code=404, detail="account not found")
        raw = await file.read()
        llm = LLMRouter(openai_api_key=_user_key(user), use_global_keys=True)
        try:
            txns = parse_pdf_preview_only(raw, password=password, llm=llm,
                                          account_type=acc.get("account_type", ""))
        except PasswordRequired as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"transactions": txns, "count": len(txns)}
    finally:
        fe.close()


@router.post("/api/finance/accounts/{aid}/upload/csv")
async def upload_csv(
    aid: str,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
):
    """
    Upload a CSV bank statement for an account.

    - If no column map is saved for this bank yet, returns
      `{"needs_mapping": True, "headers": [...], "sample_rows": [...]}`.
    - Once a column map is saved (via the /column-map endpoint) or supplied
      in a prior call, transactions are imported and
      `{"imported": N, "skipped": N, "errors": [...]}` is returned.
    """
    from ...finance.sync.csv_import import CSVImportProvider
    fe = _finance_db(user)
    cdb = _open_collab(user)
    try:
        acc = fe.get_account(aid)
        if acc is None:
            raise HTTPException(status_code=404, detail="account not found")
        raw = await file.read()
        provider = CSVImportProvider()
        result = provider.import_from_bytes(raw, fe, aid, filename=file.filename or "")
        if isinstance(result, dict):
            return result   # needs_mapping preview
        from ...finance.custodial import emit_refill_events
        from ...events.store import EventStore
        emit_refill_events(fe, EventStore(cdb), result.transactions)
        d = result.to_dict()
        _emit_fin(user, "finance.csv_imported", {
            "account_id": aid,
            "bank_name": acc.get("bank_name", ""),
            "filename": file.filename or "",
            "imported": d.get("imported", 0),
            "skipped": d.get("skipped", 0),
        })
        return d
    finally:
        fe.close()
        cdb.close()


@router.post("/api/finance/accounts/{aid}/column-map")
def save_column_map(aid: str, body: ColumnMapBody,
                    user: User = Depends(current_user)):
    """Persist a column mapping for this account's bank."""
    fe = _finance_db(user)
    try:
        acc = fe.get_account(aid)
        if acc is None:
            raise HTTPException(status_code=404, detail="account not found")
        fe.save_column_map(acc["bank_name"], body.column_map)
        return {"ok": True, "bank_name": acc["bank_name"]}
    finally:
        fe.close()


@router.get("/api/finance/column-maps")
def list_column_maps(user: User = Depends(current_user)):
    """List all saved column maps (one per bank)."""
    fe = _finance_db(user)
    try:
        return {"column_maps": fe.list_column_maps()}
    finally:
        fe.close()


@router.get("/api/finance/bank-presets")
def list_bank_presets():
    """List all built-in bank CSV format presets (no auth required)."""
    from ...finance.sync.bank_presets import list_presets
    return {"presets": list_presets()}


@router.get("/api/finance/forecast/cashflow")
def cashflow_forecast(user: User = Depends(current_user)):
    """
    Project next-week spending from the last two 7-day windows.
    Returns alert=True if projected spend exceeds monthly_income/4 × 1.1.
    """
    from ...engines.predictive_engine import PredictiveEngine
    fe = _finance_db(user)
    try:
        return PredictiveEngine(None).forecast_finance(fe)
    finally:
        fe.close()


# ---------------------------------------------------------------------------
# PDF import (B2)
# ---------------------------------------------------------------------------

@router.post("/api/finance/accounts/{aid}/upload/pdf")
async def upload_pdf(
    aid: str,
    file: UploadFile = File(...),
    password: str | None = None,
    user: User = Depends(current_user),
):
    """
    Upload a PDF bank statement for an account.
    Text is extracted via PyMuPDF and parsed by LLM.
    Pass ?password=<stmt_password> for password-protected PDFs.
    Returns {"imported": N, "skipped": N, "errors": [...]}
    """
    from ...finance.sync.pdf_import import PDFImportProvider, PasswordRequired
    from ...llm import LLMRouter
    from ..deps import _user_key
    fe = _finance_db(user)
    cdb = _open_collab(user)
    try:
        acc = fe.get_account(aid)
        if acc is None:
            raise HTTPException(status_code=404, detail="account not found")
        raw = await file.read()
        llm = LLMRouter(openai_api_key=_user_key(user), use_global_keys=False)
        provider = PDFImportProvider()
        try:
            result = provider.import_from_bytes(raw, fe, aid, llm=llm, password=password)
        except PasswordRequired as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        from ...finance.custodial import emit_refill_events
        from ...events.store import EventStore
        emit_refill_events(fe, EventStore(cdb), result.transactions)
        d = result.to_dict()
        _emit_fin(user, "finance.pdf_imported", {
            "account_id": aid,
            "bank_name": acc.get("bank_name", ""),
            "filename": file.filename or "",
            "imported": d.get("imported", 0),
            "skipped": d.get("skipped", 0),
        })
        return d
    finally:
        fe.close()
        cdb.close()


# ---------------------------------------------------------------------------
# Gmail sync (B2)
# ---------------------------------------------------------------------------

@router.post("/api/finance/accounts/{aid}/sync/gmail")
def sync_gmail(
    aid: str,
    since: str | None = None,
    until: str | None = None,
    max_messages: int = 200,
    user: User = Depends(current_user),
):
    """
    Parse bank-alert / e-statement emails from Gmail and import transactions.
    CC transactions are automatically routed to a dedicated credit card account.
    """
    from ...finance.sync.gmail_import import sync_gmail as _sync
    from ...connectors.google import load_credentials, TOKEN_FILENAME
    from ...llm import LLMRouter
    from ..deps import _user_key, _connector_dir
    fe = _finance_db(user)
    cdb = _open_collab(user)
    try:
        savings_acc = fe.get_account(aid)
        if savings_acc is None:
            raise HTTPException(status_code=404, detail="account not found")
        token_path = str(_connector_dir(user) / TOKEN_FILENAME)
        creds = load_credentials(token_path)
        if creds is None:
            raise HTTPException(status_code=403,
                detail="Google account not linked. Go to Account → Google and connect.")

        # Find or auto-create a credit card account for this bank
        bank = savings_acc["bank_name"]
        rows = fe.conn.execute(
            "SELECT id FROM accounts WHERE bank_name=? AND account_type='credit_card' LIMIT 1",
            (bank,)
        ).fetchone()
        if rows:
            cc_aid = rows[0]
        else:
            cc_aid = fe.add_account(
                nickname=f"{bank} Credit Card",
                bank_name=bank,
                account_type="credit_card",
            )

        llm = LLMRouter(openai_api_key=_user_key(user), use_global_keys=True)
        result = _sync(creds, fe, aid, llm,
                       since=since, until=until,
                       max_messages=max_messages,
                       cc_account_id=cc_aid)

        from ...finance.custodial import emit_refill_events
        from ...events.store import EventStore
        emit_refill_events(fe, EventStore(cdb), result.transactions)

        d = result.to_dict()
        if d.get("imported", 0) > 0:
            _emit_fin(user, "finance.gmail_synced", {
                "imported": d["imported"],
                "skipped": d.get("skipped", 0),
                "accounts_synced": 1,
            })
        return d
    finally:
        fe.close()
        cdb.close()


@router.post("/api/finance/sync/gmail")
def sync_gmail_all(
    since: str | None = None,
    until: str | None = None,
    max_messages: int = 500,
    user: User = Depends(current_user),
):
    """
    Global Gmail sync — scans all savings/checking accounts for this user in one call.
    CC transactions are auto-routed to a per-bank credit card account.
    Default window: caller passes since= (e.g. 30 days for initial, today for auto-poll).
    """
    from ...finance.sync.gmail_import import sync_gmail as _sync, SyncResult
    from ...connectors.google import load_credentials, TOKEN_FILENAME
    from ...llm import LLMRouter
    from ..deps import _user_key, _connector_dir

    token_path = str(_connector_dir(user) / TOKEN_FILENAME)
    creds = load_credentials(token_path)
    if creds is None:
        raise HTTPException(status_code=403,
            detail="Google account not linked. Go to Account → Google and connect.")

    fe = _finance_db(user)
    cdb = _open_collab(user)
    try:
        # Sync every non-CC, non-investment account (savings + current +
        # custodial — a custodial account gets bank alerts too, just for
        # money that isn't the user's own; see amy/finance/custodial.py)
        accounts = fe.list_accounts()
        targets = [a for a in accounts
                   if a.get("account_type") in ("savings", "current", "custodial", None, "")]

        if not targets:
            # No savings account yet — use the first account of any type
            targets = accounts[:1]

        if not targets:
            return {"imported": 0, "skipped": 0, "errors": ["No accounts found. Add one first."]}

        llm = LLMRouter(openai_api_key=_user_key(user), use_global_keys=True)

        from datetime import date, timedelta
        today_str = date.today().isoformat()
        default_since = (date.today() - timedelta(days=30)).isoformat()

        # Accumulate results across all accounts
        total_imported = total_skipped = 0
        all_errors: list = []
        all_transactions: list = []

        for acc in targets:
            aid = acc["id"]
            bank = acc.get("bank_name", "Bank")

            # Find or auto-create CC account for this bank
            row = fe.conn.execute(
                "SELECT id FROM accounts"
                " WHERE bank_name=? AND account_type='credit_card' LIMIT 1", (bank,)
            ).fetchone()
            cc_aid = row[0] if row else fe.add_account(
                nickname=f"{bank} Credit Card",
                bank_name=bank,
                account_type="credit_card",
            )

            # If the caller (auto-poll) didn't pin a since date, resume from this
            # account's last successful sync instead of assuming "today" — closes
            # the gap left by any downtime (server restart, closed tab, etc).
            # Dedup on insert is content-based (date+amount+merchant), so widening
            # the window here never re-imports anything already recorded.
            if since:
                acc_since, acc_max = since, max_messages
            else:
                last = acc.get("last_synced_at")
                acc_since = last[:10] if last else default_since
                acc_max = max_messages if acc_since >= today_str else max(max_messages, 300)

            r = _sync(creds, fe, aid, llm,
                      since=acc_since, until=until,
                      max_messages=acc_max,
                      cc_account_id=cc_aid)
            total_imported += r.imported
            total_skipped  += r.skipped
            all_errors.extend(r.errors)
            all_transactions.extend(r.transactions)

        from ...finance.custodial import emit_refill_events
        from ...events.store import EventStore
        emit_refill_events(fe, EventStore(cdb), all_transactions)

        result = {
            "imported":     total_imported,
            "skipped":      total_skipped,
            "errors":       all_errors,
            "transactions": all_transactions,
        }
        if total_imported > 0:
            _emit_fin(user, "finance.gmail_synced", {
                "imported": total_imported,
                "skipped": total_skipped,
                "accounts_synced": len(targets),
            })
        return result
    finally:
        fe.close()
        cdb.close()


@router.get("/api/finance/gmail/scope-status")
def gmail_scope_status(user: User = Depends(current_user)):
    """
    Report whether Gmail access is available for this user.

    gmail.readonly is already included in the Google connector's OAuth scope list.
    If the user has a linked Google account, Gmail sync is immediately available.
    """
    from ...connectors.google import load_credentials, TOKEN_FILENAME, SCOPES
    from ..deps import _connector_dir
    token_path = str(_connector_dir(user) / TOKEN_FILENAME)
    creds = load_credentials(token_path)
    return {
        "gmail_scope_in_oauth_flow": "https://www.googleapis.com/auth/gmail.readonly" in SCOPES,
        "google_linked": creds is not None,
        "can_sync_gmail": creds is not None,
        "re_consent_required": False,
        "note": (
            "gmail.readonly has always been part of the Google connector OAuth scope. "
            "No re-consent is needed for existing linked accounts."
        ),
    }


# ---------------------------------------------------------------------------
# Investment CSV import (B2)
# ---------------------------------------------------------------------------

@router.post("/api/finance/accounts/{aid}/upload/investments/csv")
async def upload_investments_csv(
    aid: str,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
):
    """
    Upload a portfolio CSV (mutual funds, stocks, etc.).

    - First upload with no saved column map returns a preview (needs_mapping).
    - Save the map via POST /api/finance/accounts/{id}/column-map then re-upload.
    - Subsequent uploads auto-apply the saved map (UPSERT by fund/stock name).
    """
    from ...finance.sync.investment_csv import InvestmentCSVProvider
    fe = _finance_db(user)
    try:
        if fe.get_account(aid) is None:
            raise HTTPException(status_code=404, detail="account not found")
        raw = await file.read()
        provider = InvestmentCSVProvider()
        result = provider.import_from_bytes(raw, fe, account_id=aid)
        if isinstance(result, dict):
            return result   # needs_mapping preview
        return result.to_dict()
    finally:
        fe.close()


# ---------------------------------------------------------------------------
# AA stub (B2)
# ---------------------------------------------------------------------------

def _aa_enabled(user: User) -> bool:
    return bool(user.aa_enabled if user.aa_enabled is not None else True)


@router.get("/api/finance/accounts/{aid}/sync/aa/status")
def aa_status(aid: str, user: User = Depends(current_user)):
    """
    Report Account Aggregator configuration status for this account.
    Returns what env vars are missing and step-by-step setup instructions.
    Also reflects whether the user has AA enabled in their settings.
    """
    from ...finance.sync.aa import AAProvider
    aa_on = _aa_enabled(user)
    fe = _finance_db(user)
    try:
        if fe.get_account(aid) is None:
            raise HTTPException(status_code=404, detail="account not found")
        status = AAProvider().status()
        status["aa_enabled_in_settings"] = aa_on
        if not aa_on:
            status["note"] = ("Account Aggregator is disabled in your settings. "
                              "Enable it from Settings → Account Aggregator.")
        return status
    finally:
        fe.close()


@router.post("/api/finance/calendar/sync")
def sync_calendar(days: int = 30, user: User = Depends(current_user)):
    """
    Push upcoming finance due-dates (bills, subscription renewals) into Google Calendar.
    Requires Google account to be linked via the connectors section.
    Returns {"created": N, "skipped": N, "errors": [...]}
    """
    from ...agents.calendar import CalendarAgent
    from ..deps import _connector_dir
    fe = _finance_db(user)
    try:
        agent = CalendarAgent(
            finance_db_path=str(paths.index_dir(user.id) / "finance.db"),
            connector_dir=str(_connector_dir(user)),
        )
        return agent.push_finance_events_to_calendar(days=days)
    finally:
        fe.close()


@router.post("/api/finance/accounts/{aid}/sync/aa")
def sync_aa(aid: str, consent_handle: str | None = None,
            user: User = Depends(current_user)):
    """
    Initiate an Account Aggregator data fetch.
    Returns 503 with setup instructions until AA credentials are configured,
    or 403 if the user has disabled AA in their settings.
    """
    from ...finance.sync.aa import AAProvider, AANotConfiguredError
    if not _aa_enabled(user):
        raise HTTPException(
            status_code=403,
            detail="Account Aggregator is disabled in your settings. "
                   "Enable it from Settings → Account Aggregator.")
    fe = _finance_db(user)
    try:
        if fe.get_account(aid) is None:
            raise HTTPException(status_code=404, detail="account not found")
        try:
            result = AAProvider().sync(fe, aid, consent_handle=consent_handle)
            return result.to_dict()
        except AANotConfiguredError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except NotImplementedError as exc:
            raise HTTPException(status_code=501, detail=str(exc))
    finally:
        fe.close()


# ---------------------------------------------------------------------------
# Custodial accounts (e.g. an SBI account held in-trust, refilled by someone
# else and forwarded to a fixed set of beneficiaries). Never moves money —
# only detects refills, prompts, logs a transfer the user already sent, and
# validates. See amy/finance/custodial.py and custodial_sheets.py.
# ---------------------------------------------------------------------------

class BeneficiaryBody(BaseModel):
    name: str
    split_kind: str = "single"
    default_parts: list = []
    sheet_tab: str | None = None


class DisburseBody(BaseModel):
    beneficiary_id: str
    amount: float
    date: str | None = None
    mode: str = "NEFT"
    category: str = "Custodial Disbursement"
    notes: str = ""
    part: str | None = None   # split part label (e.g. "Personal" / "MJVR")


class SheetLinkBody(BaseModel):
    sheet: str  # full Google Sheets URL or bare spreadsheet ID


class SheetImportBody(BaseModel):
    tabs: list[str] | None = None  # None = every tab that has parseable rows


class PrecheckBody(BaseModel):
    beneficiary_id: str
    amount: float
    part: str | None = None


class SuggestionConfirmBody(BaseModel):
    beneficiary_id: str
    mode: str = "UPI"


def _require_custodial_account(fe, account_id: str) -> dict:
    acc = fe.get_account(account_id)
    if not acc or acc.get("account_type") != "custodial":
        raise HTTPException(status_code=404, detail="custodial account not found")
    return acc


def _notify_low_balance(user: "User", fe, account: dict) -> dict | None:
    """After money moves: raise the balance-driven refill notification
    (deduped daily) and return the shortfall for the API response so the UI
    can flag it immediately. Fire-and-forget — never breaks the route."""
    try:
        from ...finance.custodial import check_low_balance_refill, cycle_commitment
        cdb = _open_collab(user)
        try:
            from ...notifications import NotificationStore
            check_low_balance_refill(fe, NotificationStore(cdb), account)
        finally:
            cdb.close()
        c = cycle_commitment(fe, account["id"])
        bal = fe.custodial_balance(account["id"])
        if c["total"] > 0 and bal < c["total"]:
            return {"balance": bal, "commitment": c["total"],
                    "shortfall": round(c["total"] - bal, 2)}
    except Exception:
        pass
    return None


def _maybe_close_cycle(user: "User", fe, account: dict) -> None:
    """After a disbursement lands: if every beneficiary is now paid this
    cycle, write a narrative note into the vault (idempotent by filename) and
    raise a notification (deduped 24h). Fire-and-forget — never breaks the
    calling route. LLM insight line is local-only (sensitive=True)."""
    try:
        import datetime as _dt
        import re as _re
        from ...finance.custodial_ai import cycle_close_status, cycle_narrative
        status = cycle_close_status(fe, account["id"])
        if not status["complete"]:
            return
        today = _dt.date.today().isoformat()
        ref_id = f"custodial_cycle_closed_{account['id']}_{today}"
        cdb = _open_collab(user)
        try:
            from ...notifications import NotificationStore
            store = NotificationStore(cdb)
            if store.exists_today("custodial_cycle_closed", ref_id):
                return
            from ...llm import LLMRouter
            from ..deps import _user_key
            llm = LLMRouter(openai_api_key=_user_key(user), use_global_keys=True)
            title, body = cycle_narrative(fe, account, status, llm=llm)
            slug = _re.sub(r"[^\w -]", "", account.get("nickname") or "account").strip()
            note_dir = tenancy.resolve_vault_dir(user.id) / "09_Memory"
            note_dir.mkdir(parents=True, exist_ok=True)
            note = note_dir / f"Custodial Cycle - {slug} {today}.md"
            if not note.exists():
                note.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
            store.create(type="custodial_cycle_closed", title=title, body=body,
                         related_entity={"id": ref_id,
                                         "entity_type": "custodial_account",
                                         "account_id": account["id"]})
        finally:
            cdb.close()
    except Exception:
        pass


@router.post("/api/finance/custodial/{account_id}/beneficiaries")
def add_custodial_beneficiary(account_id: str, body: BeneficiaryBody,
                              user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        _require_custodial_account(fe, account_id)
        bid = fe.add_beneficiary(account_id, body.name, body.split_kind,
                                 body.default_parts, body.sheet_tab)
        return {"id": bid}
    finally:
        fe.close()


@router.get("/api/finance/custodial/{account_id}/beneficiaries")
def list_custodial_beneficiaries(account_id: str, user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        _require_custodial_account(fe, account_id)
        return {"beneficiaries": fe.list_beneficiaries(account_id)}
    finally:
        fe.close()


@router.get("/api/finance/custodial/{account_id}/next-cycle-prefill")
def custodial_next_cycle_prefill(account_id: str, user: User = Depends(current_user)):
    """Due date + each beneficiary's last logged amount — the editable
    starting point the UI shows for the month-end nudge — plus current balance."""
    from ...finance.custodial import next_cycle_prefill
    fe = _finance_db(user)
    try:
        from ...finance.custodial import cycle_commitment
        _require_custodial_account(fe, account_id)
        prefill = next_cycle_prefill(fe, account_id)
        prefill["balance"] = fe.custodial_balance(account_id)
        prefill["cycle_commitment"] = cycle_commitment(fe, account_id)["total"]
        return prefill
    finally:
        fe.close()


@router.get("/api/finance/custodial/{account_id}/validate")
def custodial_validate(account_id: str, user: User = Depends(current_user)):
    """Split-sum, skipped-beneficiary, and overdue-refill checks — stateless,
    computed on demand against this user's own data. No second role/persona."""
    from ...finance.custodial import run_validation
    fe = _finance_db(user)
    try:
        _require_custodial_account(fe, account_id)
        return run_validation(fe, account_id)
    finally:
        fe.close()


@router.post("/api/finance/custodial/{account_id}/disburse")
def custodial_disburse(account_id: str, body: DisburseBody,
                       user: User = Depends(current_user)):
    """
    Records ONE beneficiary/part's transfer that the user already sent
    themselves (never initiates a transfer). Atomically: inserts the
    transaction, emits custodial.disbursed, and appends a row to the user's
    Google Sheet. If the Sheet write fails, the transaction/event are NOT
    rolled back — the ledger is the source of truth; retry the Sheet write
    separately via .../disburse/{transaction_id}/retry-sheet.
    """
    import datetime as _dt
    from ...finance.custodial_sheets import append_disbursement_row
    from ...connectors.google import load_credentials, TOKEN_FILENAME
    from ...events.store import EventStore
    from ..deps import _connector_dir

    fe = _finance_db(user)
    cdb = _open_collab(user)
    try:
        acc = _require_custodial_account(fe, account_id)
        ben = fe.get_beneficiary(body.beneficiary_id)
        if not ben or ben["account_id"] != account_id:
            raise HTTPException(status_code=404, detail="beneficiary not found")

        date = body.date or _dt.date.today().isoformat()
        tid = fe.add_transaction(
            amount=-abs(body.amount), category=body.category,
            merchant=ben["name"], date=date, source="custodial_manual",
            notes=body.notes, account_id=account_id)
        fe.conn.execute("UPDATE transactions SET beneficiary_id=?, part=? WHERE id=?",
                        (body.beneficiary_id, body.part, tid))
        fe.conn.commit()

        events = EventStore(cdb)
        eid = events.emit("custodial.disbursed", {
            "account_id": account_id, "beneficiary_id": body.beneficiary_id,
            "beneficiary_name": ben["name"], "transaction_id": tid,
            "amount": body.amount, "date": date, "mode": body.mode,
            "part": body.part,
        }, source="custodial_disburse_endpoint")

        token_path = str(_connector_dir(user) / TOKEN_FILENAME)
        creds = load_credentials(token_path)
        sheet_result = append_disbursement_row(
            creds, acc, ben, date, body.mode, body.amount, body.category,
            body.notes, part=body.part)

        _maybe_close_cycle(user, fe, acc)
        return {
            "transaction_id": tid, "event_id": eid,
            "sheet_write": sheet_result,
            "balance": fe.custodial_balance(account_id),
            "balance_warning": _notify_low_balance(user, fe, acc),
        }
    finally:
        fe.close()
        cdb.close()


@router.post("/api/finance/custodial/{account_id}/disburse/{transaction_id}/retry-sheet")
def custodial_retry_sheet(account_id: str, transaction_id: str,
                          user: User = Depends(current_user)):
    """Re-attempt just the Google Sheet write for a disbursement that was
    already logged (transaction + event) but whose Sheet append failed."""
    from ...finance.custodial_sheets import append_disbursement_row
    from ...connectors.google import load_credentials, TOKEN_FILENAME
    from ..deps import _connector_dir

    fe = _finance_db(user)
    try:
        acc = _require_custodial_account(fe, account_id)
        row = fe.conn.execute(
            "SELECT * FROM transactions WHERE id=? AND account_id=?",
            (transaction_id, account_id)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="transaction not found")
        if not row["beneficiary_id"]:
            raise HTTPException(status_code=400, detail="transaction has no linked beneficiary")
        ben = fe.get_beneficiary(row["beneficiary_id"])
        if not ben:
            raise HTTPException(status_code=404, detail="beneficiary not found")

        token_path = str(_connector_dir(user) / TOKEN_FILENAME)
        creds = load_credentials(token_path)
        return append_disbursement_row(
            creds, acc, ben, row["date"], "NEFT", abs(row["amount"]),
            row["category"], row["notes"] or "", part=row["part"])
    finally:
        fe.close()


# --- Bootstrap from an already-manually-maintained Google Sheet -------------
# The user was tracking disbursements in a Sheet (one tab per beneficiary)
# before this feature existed. link → analyze (read-only preview) → import
# (creates beneficiaries from tabs + backfills history, deduped, so cadence /
# prefill / validation work immediately). Never modifies the Sheet.

def _custodial_creds(user):
    from ...connectors.google import load_credentials, TOKEN_FILENAME
    from ..deps import _connector_dir
    return load_credentials(str(_connector_dir(user) / TOKEN_FILENAME))


def _match_beneficiary(beneficiaries: list[dict], tab: str) -> dict | None:
    key = tab.strip().lower()
    for b in beneficiaries:
        if (b.get("sheet_tab") or "").strip().lower() == key:
            return b
        if b["name"].strip().lower() == key:
            return b
    return None


def _sheet_row_exists(fe, account_id: str, beneficiary_id: str | None,
                      date: str, signed_amount: float) -> bool:
    return fe.conn.execute(
        "SELECT 1 FROM transactions WHERE account_id=? AND date=?"
        " AND ABS(amount-?)<0.01 AND COALESCE(beneficiary_id,'')=COALESCE(?,'')",
        (account_id, date, signed_amount, beneficiary_id)).fetchone() is not None


@router.post("/api/finance/custodial/{account_id}/sheet")
def custodial_link_sheet(account_id: str, body: SheetLinkBody,
                         user: User = Depends(current_user)):
    """Store the user's existing Sheet on the account (accounts.meta.sheet_id)."""
    from ...finance.custodial_sheets import SHEET_ID_META_KEY, extract_sheet_id
    sid = extract_sheet_id(body.sheet)
    if not sid:
        raise HTTPException(status_code=400, detail="could not read a spreadsheet ID from that — paste the full Sheet URL")
    fe = _finance_db(user)
    try:
        acc = _require_custodial_account(fe, account_id)
        meta = acc.get("meta") or {}
        meta[SHEET_ID_META_KEY] = sid
        fe.update_account(account_id, meta=meta)
        return {"sheet_id": sid}
    finally:
        fe.close()


@router.get("/api/finance/custodial/{account_id}/sheet/analyze")
def custodial_analyze_sheet(account_id: str, user: User = Depends(current_user)):
    """Read-only preview of the linked Sheet: per tab, how many rows parse,
    date range, totals, and how much is already in the ledger (deduped)."""
    from ...finance.custodial_sheets import SHEET_ID_META_KEY, fetch_sheet_data
    fe = _finance_db(user)
    try:
        acc = _require_custodial_account(fe, account_id)
        sheet_id = (acc.get("meta") or {}).get(SHEET_ID_META_KEY)
        if not sheet_id:
            return {"linked": False}
        data = fetch_sheet_data(_custodial_creds(user), sheet_id)
        if not data.get("ok"):
            raise HTTPException(status_code=400, detail=data.get("error", "sheet read failed"))

        beneficiaries = fe.list_beneficiaries(account_id, active_only=False)
        tabs = []
        for t in data["tabs"]:
            rows = t["rows"]
            ben = _match_beneficiary(beneficiaries, t["tab"])
            already = 0
            for r in rows:
                if r["kind"] == "account_debit":
                    continue   # import-time decision; not previewed row-by-row
                signed = r["amount"] if r["kind"] == "refill" else -r["amount"]
                bid = None if r["kind"] == "refill" else (ben["id"] if ben else None)
                if ben or r["kind"] == "refill":
                    if _sheet_row_exists(fe, account_id, bid, r["date"], signed):
                        already += 1
            tabs.append({
                "tab": t["tab"],
                "parsed": len(rows),
                "skipped": t["skipped"],
                "layout": t.get("layout", "simple"),
                "refills": sum(1 for r in rows if r["kind"] == "refill"),
                "debits_skipped": t.get("debits_skipped", 0),
                "debit_total": t.get("debit_total", 0),
                "already_imported": already,
                "first_date": rows[0]["date"] if rows else None,
                "last_date": rows[-1]["date"] if rows else None,
                "disbursed_total": round(sum(r["amount"] for r in rows if r["kind"] == "disbursement"), 2),
                "refill_total": round(sum(r["amount"] for r in rows if r["kind"] == "refill"), 2),
                "beneficiary": ben["name"] if ben else None,
            })
        return {"linked": True, "sheet_id": sheet_id,
                "sheet_title": data.get("sheet_title", ""), "tabs": tabs}
    finally:
        fe.close()


@router.post("/api/finance/custodial/{account_id}/sheet/import")
def custodial_import_sheet(account_id: str, body: SheetImportBody,
                           user: User = Depends(current_user)):
    """Bootstrap the ledger from the linked Sheet: creates a beneficiary per
    selected tab (if missing) and inserts its history as transactions
    (source='sheet_import'), skipping rows already in the ledger."""
    import uuid as _uuid
    from ...finance.custodial_sheets import SHEET_ID_META_KEY, fetch_sheet_data
    from ...finance.custodial_ai import match_beneficiary as _ai_match

    fe = _finance_db(user)
    try:
        acc = _require_custodial_account(fe, account_id)
        sheet_id = (acc.get("meta") or {}).get(SHEET_ID_META_KEY)
        if not sheet_id:
            raise HTTPException(status_code=400, detail="link a sheet first")
        data = fetch_sheet_data(_custodial_creds(user), sheet_id)
        if not data.get("ok"):
            raise HTTPException(status_code=400, detail=data.get("error", "sheet read failed"))

        beneficiaries = fe.list_beneficiaries(account_id, active_only=False)
        imported, skipped_existing, covered_skipped, created = 0, 0, 0, []
        # tab names with data — a master-log debit whose party matches one of
        # these is already covered by that tab's own rows (never double-count)
        tab_keys = [t2["tab"].strip().lower() for t2 in data["tabs"] if t2["rows"]]

        def _covered_by_tab(party: str, own_tab: str) -> bool:
            p = (party or "").strip().lower()
            return any(k != own_tab.strip().lower() and (k in p or p in k)
                       for k in tab_keys if len(k) > 2)

        for t in data["tabs"]:
            if body.tabs is not None and t["tab"] not in body.tabs:
                continue
            if not t["rows"]:
                continue
            ben = _match_beneficiary(beneficiaries, t["tab"])
            if ben is None and any(r["kind"] == "disbursement" for r in t["rows"]):
                bid = fe.add_beneficiary(account_id, t["tab"], sheet_tab=t["tab"])
                ben = fe.get_beneficiary(bid)
                beneficiaries.append(ben)
                created.append(t["tab"])
            for r in t["rows"]:
                refill = r["kind"] == "refill"
                if r["kind"] == "account_debit":
                    # master-log outflow: skip parties that have their own tab
                    # (Eswari, Sumathi); the rest (e.g. Guru IB) exist only
                    # here and are real disbursements from this account
                    if _covered_by_tab(r.get("party", ""), t["tab"]):
                        covered_skipped += 1
                        continue
                    active = [b for b in beneficiaries if b.get("active")]
                    dben, _score = _ai_match(active, r.get("party") or "")
                    if dben is None:
                        bid = fe.add_beneficiary(account_id, r["party"] or "Account outflow")
                        dben = fe.get_beneficiary(bid)
                        beneficiaries.append(dben)
                        created.append(dben["name"])
                    # same-party same-date dedup: master rows are sometimes
                    # combined totals (e.g. "pay + Eswari part"), so an exact
                    # amount match is too strict after a manual correction
                    dup = fe.conn.execute(
                        "SELECT 1 FROM transactions WHERE account_id=? AND date=?"
                        " AND beneficiary_id=?", (account_id, r["date"], dben["id"])).fetchone()
                    if dup:
                        skipped_existing += 1
                        continue
                    notes = " · ".join(x for x in (r.get("party"), r["notes"]) if x)
                    fe.conn.execute(
                        "INSERT INTO transactions(id,date,amount,category,merchant,"
                        "source,notes,account_id,beneficiary_id) VALUES(?,?,?,?,?,?,?,?,?)",
                        (_uuid.uuid4().hex, r["date"], -r["amount"], r["category"],
                         dben["name"], "sheet_import", notes, account_id, dben["id"]))
                    imported += 1
                    continue
                signed = r["amount"] if refill else -r["amount"]
                bid = None if refill else ben["id"]
                if _sheet_row_exists(fe, account_id, bid, r["date"], signed):
                    skipped_existing += 1
                    continue
                # split part from the party prefix: "Eswari Personal" in the
                # Eswari tab → part "Personal"
                part = None
                party = (r.get("party") or "").strip()
                if (not refill and party
                        and party.lower().startswith(ben["name"].strip().lower())
                        and len(party) > len(ben["name"].strip())):
                    part = party[len(ben["name"].strip()):].strip() or None
                notes = " · ".join(x for x in (r.get("party"), r["notes"]) if x)
                fe.conn.execute(
                    "INSERT INTO transactions(id,date,amount,category,merchant,"
                    "source,notes,account_id,beneficiary_id,part) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (_uuid.uuid4().hex, r["date"], signed, r["category"],
                     (r.get("party") or "Refill") if refill else ben["name"],
                     "sheet_import", notes, account_id, bid, part))
                imported += 1
        fe.conn.commit()

        _emit_fin(user, "custodial.sheet_imported", {
            "account_id": account_id, "sheet_id": sheet_id,
            "imported": imported, "skipped_existing": skipped_existing,
            "beneficiaries_created": created,
        })
        return {"imported": imported, "skipped_existing": skipped_existing,
                "covered_by_tabs": covered_skipped,
                "beneficiaries_created": created,
                "balance": fe.custodial_balance(account_id)}
    finally:
        fe.close()


class BeneficiaryPatch(BaseModel):
    name: str | None = None
    sheet_tab: str | None = None
    active: bool | None = None
    tracking_only: bool | None = None
    expected_amount: float | None = None   # static per-cycle commitment


@router.patch("/api/finance/custodial/{account_id}/beneficiaries/{beneficiary_id}")
def patch_custodial_beneficiary(account_id: str, beneficiary_id: str,
                                body: BeneficiaryPatch,
                                user: User = Depends(current_user)):
    """Edit a beneficiary — notably tracking_only: records kept in the sheet
    for bookkeeping (MJVR/VJPN-style) whose rows never count toward balance."""
    fe = _finance_db(user)
    try:
        _require_custodial_account(fe, account_id)
        ben = fe.get_beneficiary(beneficiary_id)
        if not ben or ben["account_id"] != account_id:
            raise HTTPException(status_code=404, detail="beneficiary not found")
        fields = {k: (int(v) if isinstance(v, bool) else v)
                  for k, v in body.model_dump(exclude_none=True).items()}
        fe.update_beneficiary(beneficiary_id, **fields)
        return {"beneficiary": fe.get_beneficiary(beneficiary_id),
                "balance": fe.custodial_balance(account_id)}
    finally:
        fe.close()


# --- AI layer: screenshot parse, Gmail-debit suggestions, anomaly precheck --
# All LLM text-parsing here is sensitive=True (local Ollama only). The
# screenshot OCR itself reuses the existing captures vision path (the user's
# own OpenAI key — same trust boundary as the share-intent screenshot flow).

@router.post("/api/finance/custodial/{account_id}/screenshot/parse")
async def custodial_parse_screenshot(account_id: str, file: UploadFile = File(...),
                                     user: User = Depends(current_user)):
    """Parse a UPI/NEFT transfer screenshot into a prefilled disbursement:
    OCR → regex extraction (LLM rescue if regex finds no amount) → fuzzy
    beneficiary match → anomaly warnings. Nothing is saved here; the UI
    confirms via /disburse and then links the image via /api/captures."""
    from ...captures import analyze_image, _ext_for
    from ...finance.custodial_ai import (parse_transfer_text, llm_parse_transfer,
                                         match_beneficiary, anomaly_precheck)
    from ...llm import LLMRouter
    from ..deps import _user_key

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    fe = _finance_db(user)
    try:
        _require_custodial_account(fe, account_id)
        vis = analyze_image(data, _ext_for(file.filename or "", file.content_type),
                            api_key=_user_key(user))
        ocr = "\n".join(x for x in (vis.get("ocr"), vis.get("caption")) if x)
        if not ocr.strip():
            return {"ok": False,
                    "error": "Couldn't read the image — add your OpenAI key in Account (vision OCR) or enter the transfer manually."}

        parsed = parse_transfer_text(ocr)
        used_llm = False
        if parsed["amount"] is None:
            llm = LLMRouter(openai_api_key=_user_key(user), use_global_keys=True)
            rescue = llm_parse_transfer(llm, ocr)
            for k, v in rescue.items():
                if not parsed.get(k):
                    parsed[k] = v
            used_llm = True

        bens = fe.list_beneficiaries(account_id)
        ben, score = match_beneficiary(bens, parsed.get("receiver") or ocr[:800])
        warnings = []
        if ben and parsed.get("amount"):
            warnings = anomaly_precheck(fe, account_id, ben["id"], parsed["amount"])
        return {
            "ok": True, "parsed": parsed,
            "beneficiary_id": ben["id"] if ben else None,
            "beneficiary_name": ben["name"] if ben else None,
            "match_score": score, "warnings": warnings,
            "used_llm": used_llm, "ocr_excerpt": ocr[:400],
        }
    finally:
        fe.close()


@router.get("/api/finance/custodial/{account_id}/suggestions")
def custodial_suggestions(account_id: str, user: User = Depends(current_user)):
    """Unclaimed debits already synced into this custodial account (Gmail/CSV)
    fuzzy-matched to beneficiaries — 'looks like you sent Eswari ₹5,000'."""
    from ...finance.custodial_ai import detect_disbursement_suggestions
    fe = _finance_db(user)
    try:
        _require_custodial_account(fe, account_id)
        return {"suggestions": detect_disbursement_suggestions(fe, account_id)}
    finally:
        fe.close()


@router.post("/api/finance/custodial/{account_id}/suggestions/{transaction_id}/confirm")
def custodial_confirm_suggestion(account_id: str, transaction_id: str,
                                 body: SuggestionConfirmBody,
                                 user: User = Depends(current_user)):
    """Claim an existing synced debit as a disbursement: links the beneficiary
    to the EXISTING transaction (never creates a duplicate), emits the event,
    appends the Sheet row, and checks cycle-close."""
    from ...finance.custodial_sheets import append_disbursement_row
    from ...connectors.google import load_credentials, TOKEN_FILENAME
    from ...events.store import EventStore
    from ..deps import _connector_dir

    fe = _finance_db(user)
    cdb = _open_collab(user)
    try:
        acc = _require_custodial_account(fe, account_id)
        row = fe.conn.execute(
            "SELECT * FROM transactions WHERE id=? AND account_id=?",
            (transaction_id, account_id)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="transaction not found")
        if row["amount"] >= 0:
            raise HTTPException(status_code=400, detail="not a debit")
        if row["beneficiary_id"]:
            raise HTTPException(status_code=400, detail="already linked to a beneficiary")
        ben = fe.get_beneficiary(body.beneficiary_id)
        if not ben or ben["account_id"] != account_id:
            raise HTTPException(status_code=404, detail="beneficiary not found")

        fe.conn.execute(
            "UPDATE transactions SET beneficiary_id=?, category=? WHERE id=?",
            (body.beneficiary_id, "Custodial Disbursement", transaction_id))
        fe.conn.commit()

        eid = EventStore(cdb).emit("custodial.disbursed", {
            "account_id": account_id, "beneficiary_id": body.beneficiary_id,
            "beneficiary_name": ben["name"], "transaction_id": transaction_id,
            "amount": abs(row["amount"]), "date": row["date"], "mode": body.mode,
            "detected_from": row["source"],
        }, source="custodial_suggestion_confirm")

        creds = load_credentials(str(_connector_dir(user) / TOKEN_FILENAME))
        sheet_result = append_disbursement_row(
            creds, acc, ben, row["date"], body.mode, abs(row["amount"]),
            "Custodial Disbursement", row["notes"] or "auto-detected from bank alert")

        _maybe_close_cycle(user, fe, acc)
        return {"transaction_id": transaction_id, "event_id": eid,
                "sheet_write": sheet_result,
                "balance": fe.custodial_balance(account_id),
                "balance_warning": _notify_low_balance(user, fe, acc)}
    finally:
        fe.close()
        cdb.close()


@router.post("/api/finance/custodial/{account_id}/precheck")
def custodial_precheck(account_id: str, body: PrecheckBody,
                       user: User = Depends(current_user)):
    """Soft anomaly warnings (duplicate this cycle, unusual amount, balance
    shortfall) shown before Confirm — advisory only, never blocks."""
    from ...finance.custodial_ai import anomaly_precheck
    fe = _finance_db(user)
    try:
        _require_custodial_account(fe, account_id)
        ben = fe.get_beneficiary(body.beneficiary_id)
        if not ben or ben["account_id"] != account_id:
            raise HTTPException(status_code=404, detail="beneficiary not found")
        return {"warnings": anomaly_precheck(fe, account_id,
                                             body.beneficiary_id, body.amount,
                                             part=body.part)}
    finally:
        fe.close()
