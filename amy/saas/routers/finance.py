"""Finance CFO routes — transactions, budgets, subscriptions, investments, income, afford,
accounts, and CSV import."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from ..db import User
from .. import paths
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
        return {"id": tid}
    finally:
        fe.close()


@router.get("/api/finance/transactions")
def list_transactions(limit: int = 500, category: str | None = None,
                      since: str | None = None, until: str | None = None,
                      user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        return {"transactions": fe.list_transactions(limit, category, since, until)}
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
def reset_all_transactions(user: User = Depends(current_user)):
    """Delete ALL transactions for this user (irreversible reset)."""
    fe = _finance_db(user)
    try:
        count = fe.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        fe.conn.execute("DELETE FROM transactions")
        fe.conn.commit()
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
    """Run rule-based categorization on all Uncategorized transactions."""
    from ...finance.categorizer import auto_categorize_all
    fe = _finance_db(user)
    try:
        updated = auto_categorize_all(fe)
        return {"updated": updated}
    finally:
        fe.close()


@router.patch("/api/finance/transactions/{tid}/category")
def set_category(tid: str, body: dict, user: User = Depends(current_user)):
    """Manually set category for a single transaction."""
    cat = body.get("category", "Uncategorized")
    fe = _finance_db(user)
    try:
        fe.conn.execute("UPDATE transactions SET category=? WHERE id=?", (cat, tid))
        fe.conn.commit()
        return {"ok": True}
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
        return _can_afford(body.amount, body.description, fe, collab_db=cdb)
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
        txns = parse_csv_preview_only(raw, column_map, filename)
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
        if fe.get_account(aid) is None:
            raise HTTPException(status_code=404, detail="account not found")
        raw = await file.read()
        llm = LLMRouter(openai_api_key=_user_key(user), use_global_keys=True)
        try:
            txns = parse_pdf_preview_only(raw, password=password, llm=llm)
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
    try:
        if fe.get_account(aid) is None:
            raise HTTPException(status_code=404, detail="account not found")
        raw = await file.read()
        provider = CSVImportProvider()
        result = provider.import_from_bytes(raw, fe, aid, filename=file.filename or "")
        if isinstance(result, dict):
            return result   # needs_mapping preview
        return result.to_dict()
    finally:
        fe.close()


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
    try:
        if fe.get_account(aid) is None:
            raise HTTPException(status_code=404, detail="account not found")
        raw = await file.read()
        llm = LLMRouter(openai_api_key=_user_key(user), use_global_keys=False)
        provider = PDFImportProvider()
        try:
            result = provider.import_from_bytes(raw, fe, aid, llm=llm, password=password)
        except PasswordRequired as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return result.to_dict()
    finally:
        fe.close()


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
        return result.to_dict()
    finally:
        fe.close()


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
    try:
        # Sync every non-CC, non-investment account (savings + current)
        accounts = fe.list_accounts()
        targets = [a for a in accounts
                   if a.get("account_type") in ("savings", "current", None, "")]

        if not targets:
            # No savings account yet — use the first account of any type
            targets = accounts[:1]

        if not targets:
            return {"imported": 0, "skipped": 0, "errors": ["No accounts found. Add one first."]}

        llm = LLMRouter(openai_api_key=_user_key(user), use_global_keys=True)

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

            r = _sync(creds, fe, aid, llm,
                      since=since, until=until,
                      max_messages=max_messages,
                      cc_account_id=cc_aid)
            total_imported += r.imported
            total_skipped  += r.skipped
            all_errors.extend(r.errors)
            all_transactions.extend(r.transactions)

        return {
            "imported":     total_imported,
            "skipped":      total_skipped,
            "errors":       all_errors,
            "transactions": all_transactions,
        }
    finally:
        fe.close()


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
