"""Per-user finance data store.

One SQLite file per user (finance.db), same multi-tenant pattern as habits.db/srs.db.
Tables: transactions, budgets, subscriptions, investments, income_sources,
        accounts, bank_column_maps.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import uuid
from pathlib import Path

VALID_ACCOUNT_TYPES = {"savings", "checking", "credit_card", "loan", "investment", "custodial"}
VALID_SYNC_METHODS  = {"manual", "csv", "pdf", "gmail", "aa"}
VALID_CONSTITUTIONS = {"proprietorship", "partnership", "llp", "company"}
VALID_TRACKING_CLOSENESS = {"close", "loose"}


def _uuid() -> str:
    return uuid.uuid4().hex


def _to_monthly_amount(amount: float, recurrence: str) -> float:
    if recurrence == "monthly":
        return amount
    if recurrence == "annual":
        return amount / 12
    if recurrence == "weekly":
        return amount * 52 / 12
    return amount


def _today() -> str:
    return _dt.date.today().isoformat()


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _this_month_range() -> tuple[str, str]:
    d = _dt.date.today()
    start = d.replace(day=1)
    if d.month == 12:
        end = d.replace(year=d.year + 1, month=1, day=1) - _dt.timedelta(days=1)
    else:
        end = d.replace(month=d.month + 1, day=1) - _dt.timedelta(days=1)
    return start.isoformat(), end.isoformat()


class FinanceEngine:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self._init()
        self._migrate()

    def _init(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS transactions (
                id         TEXT PRIMARY KEY,
                date       TEXT NOT NULL,
                amount     REAL NOT NULL,
                category   TEXT NOT NULL DEFAULT 'Uncategorized',
                merchant   TEXT DEFAULT '',
                source     TEXT DEFAULT 'manual',
                notes      TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS budgets (
                category      TEXT PRIMARY KEY,
                monthly_limit REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                id             TEXT PRIMARY KEY,
                name           TEXT NOT NULL,
                monthly_cost   REAL DEFAULT 0,
                annual_cost    REAL DEFAULT 0,
                renewal_date   TEXT,
                auto_renew     INTEGER DEFAULT 1,
                payment_method TEXT DEFAULT '',
                status         TEXT DEFAULT 'active'
            );
            CREATE TABLE IF NOT EXISTS investments (
                id            TEXT PRIMARY KEY,
                type          TEXT NOT NULL,
                name          TEXT NOT NULL,
                current_value REAL DEFAULT 0,
                cost_basis    REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS income_sources (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                type       TEXT DEFAULT 'salary',
                amount     REAL NOT NULL,
                recurrence TEXT DEFAULT 'monthly'
            );
            CREATE TABLE IF NOT EXISTS accounts (
                id             TEXT PRIMARY KEY,
                nickname       TEXT NOT NULL,
                bank_name      TEXT NOT NULL,
                account_type   TEXT NOT NULL DEFAULT 'savings',
                sync_method    TEXT NOT NULL DEFAULT 'manual',
                last_synced_at TEXT,
                created_at     TEXT NOT NULL,
                meta           TEXT DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS bank_column_maps (
                bank_name  TEXT PRIMARY KEY,
                column_map TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS beneficiaries (
                id            TEXT PRIMARY KEY,
                account_id    TEXT NOT NULL,
                name          TEXT NOT NULL,
                split_kind    TEXT NOT NULL DEFAULT 'single',
                default_parts TEXT NOT NULL DEFAULT '[]',
                sheet_tab     TEXT,
                active        INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS business_entities (
                id                       TEXT PRIMARY KEY,
                name                     TEXT NOT NULL,
                pan                      TEXT,
                gstin                    TEXT,
                constitution             TEXT NOT NULL DEFAULT 'proprietorship',
                registration_state       TEXT,
                financial_year           TEXT,
                tax_regime               TEXT,
                holds_depreciable_assets INTEGER NOT NULL DEFAULT 0,
                tracking_closeness       TEXT NOT NULL DEFAULT 'loose',
                created_at               TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ledger_entries (
                id                 TEXT PRIMARY KEY,
                business_entity_id TEXT NOT NULL,
                date               TEXT NOT NULL,
                amount             REAL NOT NULL,
                description        TEXT DEFAULT '',
                category           TEXT DEFAULT 'Uncategorized',
                source_event_id    TEXT NOT NULL,
                source_document    TEXT,
                confidence         REAL DEFAULT 1.0,
                posted_by          TEXT NOT NULL DEFAULT 'accountant',
                audit_status       TEXT DEFAULT 'unaudited',
                created_at         TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS compliance_suggestions (
                id                  TEXT PRIMARY KEY,
                business_entity_id  TEXT NOT NULL,
                ledger_entry_id     TEXT NOT NULL,
                source_event_id     TEXT NOT NULL,
                suggestion_type     TEXT NOT NULL,
                reasoning           TEXT NOT NULL,
                rate_used           TEXT,
                citation            TEXT NOT NULL,
                ca_disclaimer       TEXT NOT NULL DEFAULT 'Confirm with your CA before acting on this.',
                routed_sensitive    INTEGER NOT NULL DEFAULT 0,
                created_at          TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rate_table (
                id             TEXT PRIMARY KEY,
                rate_type      TEXT NOT NULL,
                key            TEXT NOT NULL,
                value          TEXT NOT NULL,
                effective_from TEXT NOT NULL,
                effective_to   TEXT,
                source_note    TEXT DEFAULT '',
                updated_at     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_txn_date  ON transactions(date);
            CREATE INDEX IF NOT EXISTS idx_txn_cat   ON transactions(category);
            CREATE INDEX IF NOT EXISTS idx_sub_renew ON subscriptions(renewal_date);
            CREATE INDEX IF NOT EXISTS idx_ben_account ON beneficiaries(account_id);
            CREATE INDEX IF NOT EXISTS idx_ledger_entity ON ledger_entries(business_entity_id);
            CREATE INDEX IF NOT EXISTS idx_ledger_source_event ON ledger_entries(source_event_id);
            CREATE INDEX IF NOT EXISTS idx_compliance_entity ON compliance_suggestions(business_entity_id);
            CREATE INDEX IF NOT EXISTS idx_compliance_ledger_entry ON compliance_suggestions(ledger_entry_id);
            CREATE INDEX IF NOT EXISTS idx_rate_type_key ON rate_table(rate_type, key);
        """)
        self.conn.commit()

    def _migrate(self):
        """Apply incremental schema changes to existing databases."""
        # Add account_id column to transactions (idempotent)
        try:
            self.conn.execute("ALTER TABLE transactions ADD COLUMN account_id TEXT")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        # Index on account_id must come after the column exists
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_txn_account ON transactions(account_id)")
        self.conn.commit()
        # Custodial-account support: which beneficiary a disbursement was for,
        # and an optional path to an attached UPI/NEFT confirmation screenshot.
        for col, coltype in (("beneficiary_id", "TEXT"), ("screenshot_path", "TEXT")):
            try:
                self.conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} {coltype}")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_txn_beneficiary ON transactions(beneficiary_id)")
        self.conn.commit()
        # Jurisdiction packs + multi-currency (R7B): optional jurisdiction on
        # accounts/business entities (default = user's home pack at read time)
        # and a native currency per account/transaction (default = home pack
        # currency at read time — NULL means "home").
        for table, col in (("accounts", "jurisdiction"),
                           ("accounts", "currency"),
                           ("transactions", "currency"),
                           ("business_entities", "jurisdiction")):
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
        # Business entities: seed the rate table with a starter set of GST
        # slabs and depreciation blocks (idempotent — only inserts missing keys).
        from .business.rates import seed_defaults as _seed_rate_defaults
        _seed_rate_defaults(self)

    # =========================================================================
    # Transactions
    # =========================================================================

    def add_transaction(self, amount: float, category: str, merchant: str = "",
                        date: str | None = None, source: str = "manual",
                        notes: str = "", account_id: str | None = None) -> str:
        tid = _uuid()
        self.conn.execute(
            "INSERT INTO transactions"
            "(id,date,amount,category,merchant,source,notes,account_id)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (tid, date or _today(), amount, category, merchant, source, notes, account_id))
        self.conn.commit()
        return tid

    def list_transactions(self, limit: int = 100, category: str | None = None,
                          since: str | None = None, until: str | None = None,
                          account_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM transactions WHERE 1=1"
        params: list = []
        if category:
            q += " AND category=?"; params.append(category)
        if since:
            q += " AND date>=?"; params.append(since)
        if until:
            q += " AND date<=?"; params.append(until)
        if account_id:
            q += " AND account_id=?"; params.append(account_id)
        q += " ORDER BY date DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]

    def delete_transaction(self, tid: str) -> bool:
        c = self.conn.execute("DELETE FROM transactions WHERE id=?", (tid,))
        self.conn.commit()
        return c.rowcount > 0

    # =========================================================================
    # Budgets
    # =========================================================================

    def set_budget(self, category: str, monthly_limit: float):
        self.conn.execute(
            "INSERT OR REPLACE INTO budgets(category,monthly_limit) VALUES(?,?)",
            (category, monthly_limit))
        self.conn.commit()

    def list_budgets(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM budgets ORDER BY category")]

    def delete_budget(self, category: str) -> bool:
        c = self.conn.execute("DELETE FROM budgets WHERE category=?", (category,))
        self.conn.commit()
        return c.rowcount > 0

    # =========================================================================
    # Subscriptions
    # =========================================================================

    def add_subscription(self, name: str, monthly_cost: float = 0,
                         annual_cost: float = 0, renewal_date: str | None = None,
                         auto_renew: bool = True, payment_method: str = "",
                         status: str = "active") -> str:
        sid = _uuid()
        self.conn.execute(
            "INSERT INTO subscriptions(id,name,monthly_cost,annual_cost,"
            "renewal_date,auto_renew,payment_method,status) VALUES(?,?,?,?,?,?,?,?)",
            (sid, name, monthly_cost, annual_cost, renewal_date,
             int(auto_renew), payment_method, status))
        self.conn.commit()
        return sid

    def list_subscriptions(self, status: str | None = "active") -> list[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM subscriptions WHERE status=? ORDER BY name",
                (status,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM subscriptions ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def update_subscription(self, sid: str, **kwargs) -> bool:
        allowed = {"name", "monthly_cost", "annual_cost", "renewal_date",
                   "auto_renew", "payment_method", "status"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return False
        sets = ", ".join(f"{k}=?" for k in fields)
        c = self.conn.execute(
            f"UPDATE subscriptions SET {sets} WHERE id=?",
            list(fields.values()) + [sid])
        self.conn.commit()
        return c.rowcount > 0

    def delete_subscription(self, sid: str) -> bool:
        c = self.conn.execute("DELETE FROM subscriptions WHERE id=?", (sid,))
        self.conn.commit()
        return c.rowcount > 0

    def subscription_insights(self) -> dict:
        subs = self.list_subscriptions()
        monthly_total = self.subscription_total_monthly()
        annual_cost = round(monthly_total * 12, 2)
        name_groups: dict[str, list] = {}
        for s in subs:
            first_word = (s["name"].lower().split()[0]
                          if s["name"].strip() else s["name"].lower())
            name_groups.setdefault(first_word, []).append(s)
        duplicate_suspects = [
            {"names": [s["name"] for s in group],
             "combined_monthly": round(sum(s["monthly_cost"] for s in group), 2)}
            for group in name_groups.values() if len(group) > 1
        ]
        return {
            "total_subscriptions": len(subs),
            "monthly_total": round(monthly_total, 2),
            "annual_cost": annual_cost,
            "duplicate_suspects": duplicate_suspects,
            "subscriptions": subs,
        }

    # =========================================================================
    # Investments
    # =========================================================================

    def add_investment(self, inv_type: str, name: str, current_value: float = 0,
                       cost_basis: float = 0) -> str:
        iid = _uuid()
        self.conn.execute(
            "INSERT INTO investments(id,type,name,current_value,cost_basis) VALUES(?,?,?,?,?)",
            (iid, inv_type, name, current_value, cost_basis))
        self.conn.commit()
        return iid

    def list_investments(self) -> list[dict]:
        return [dict(r) for r in
                self.conn.execute("SELECT * FROM investments ORDER BY name").fetchall()]

    def update_investment(self, iid: str, current_value: float | None = None,
                          cost_basis: float | None = None,
                          type: str | None = None) -> bool:
        sets, params = [], []
        if current_value is not None:
            sets.append("current_value=?"); params.append(current_value)
        if cost_basis is not None:
            sets.append("cost_basis=?"); params.append(cost_basis)
        if type is not None:
            sets.append("type=?"); params.append(type)
        if not sets:
            return False
        params.append(iid)
        c = self.conn.execute(
            f"UPDATE investments SET {', '.join(sets)} WHERE id=?", params)
        self.conn.commit()
        return c.rowcount > 0

    def delete_investment(self, iid: str) -> bool:
        c = self.conn.execute("DELETE FROM investments WHERE id=?", (iid,))
        self.conn.commit()
        return c.rowcount > 0

    def portfolio_summary(self) -> dict:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(current_value),0) cv, COALESCE(SUM(cost_basis),0) cb"
            " FROM investments").fetchone()
        cv, cb = row["cv"], row["cb"]
        return {
            "total_value": round(cv, 2),
            "total_cost": round(cb, 2),
            "gain_loss": round(cv - cb, 2),
            "return_pct": round((cv - cb) / cb * 100, 2) if cb else None,
            "by_type": {
                r["type"]: round(r["total"], 2)
                for r in self.conn.execute(
                    "SELECT type, SUM(current_value) total FROM investments GROUP BY type"
                ).fetchall()
            },
        }

    # =========================================================================
    # Income sources
    # =========================================================================

    def add_income_source(self, name: str, income_type: str = "salary",
                          amount: float = 0, recurrence: str = "monthly") -> str:
        sid = _uuid()
        self.conn.execute(
            "INSERT INTO income_sources(id,name,type,amount,recurrence) VALUES(?,?,?,?,?)",
            (sid, name, income_type, amount, recurrence))
        self.conn.commit()
        return sid

    def list_income_sources(self) -> list[dict]:
        return [dict(r) for r in
                self.conn.execute("SELECT * FROM income_sources ORDER BY name").fetchall()]

    def delete_income_source(self, sid: str) -> bool:
        c = self.conn.execute("DELETE FROM income_sources WHERE id=?", (sid,))
        self.conn.commit()
        return c.rowcount > 0

    # =========================================================================
    # Accounts
    # =========================================================================

    def add_account(self, nickname: str, bank_name: str,
                    account_type: str = "savings",
                    sync_method: str = "manual",
                    meta: dict | None = None) -> str:
        if account_type not in VALID_ACCOUNT_TYPES:
            raise ValueError(f"account_type must be one of {VALID_ACCOUNT_TYPES}")
        if sync_method not in VALID_SYNC_METHODS:
            raise ValueError(f"sync_method must be one of {VALID_SYNC_METHODS}")
        aid = _uuid()
        self.conn.execute(
            "INSERT INTO accounts(id,nickname,bank_name,account_type,"
            "sync_method,created_at,meta) VALUES(?,?,?,?,?,?,?)",
            (aid, nickname, bank_name, account_type, sync_method,
             _now_iso(), json.dumps(meta or {})))
        self.conn.commit()
        return aid

    def get_account(self, account_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["meta"] = json.loads(d.get("meta") or "{}")
        return d

    def list_accounts(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM accounts ORDER BY created_at").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["meta"] = json.loads(d.get("meta") or "{}")
            # attach transaction count
            cnt = self.conn.execute(
                "SELECT COUNT(*) n FROM transactions WHERE account_id=?",
                (d["id"],)).fetchone()["n"]
            d["transaction_count"] = cnt
            result.append(d)
        return result

    def update_account(self, account_id: str, **kwargs) -> bool:
        allowed = {"nickname", "bank_name", "account_type", "sync_method", "meta",
                   "jurisdiction", "currency"}
        fields = {}
        for k, v in kwargs.items():
            if k not in allowed:
                continue
            if k == "meta":
                v = json.dumps(v)
            if k == "account_type" and v not in VALID_ACCOUNT_TYPES:
                raise ValueError(f"account_type must be one of {VALID_ACCOUNT_TYPES}")
            if k == "sync_method" and v not in VALID_SYNC_METHODS:
                raise ValueError(f"sync_method must be one of {VALID_SYNC_METHODS}")
            fields[k] = v
        if not fields:
            return False
        sets = ", ".join(f"{k}=?" for k in fields)
        c = self.conn.execute(
            f"UPDATE accounts SET {sets} WHERE id=?",
            list(fields.values()) + [account_id])
        self.conn.commit()
        return c.rowcount > 0

    def delete_account(self, account_id: str) -> bool:
        # Unlink transactions but don't delete them
        self.conn.execute(
            "UPDATE transactions SET account_id=NULL WHERE account_id=?",
            (account_id,))
        c = self.conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        self.conn.commit()
        return c.rowcount > 0

    def touch_account(self, account_id: str):
        """Update last_synced_at to now."""
        self.conn.execute(
            "UPDATE accounts SET last_synced_at=? WHERE id=?",
            (_now_iso(), account_id))
        self.conn.commit()

    # =========================================================================
    # Beneficiaries (custodial accounts)
    # =========================================================================

    def add_beneficiary(self, account_id: str, name: str,
                        split_kind: str = "single",
                        default_parts: list | None = None,
                        sheet_tab: str | None = None) -> str:
        bid = _uuid()
        self.conn.execute(
            "INSERT INTO beneficiaries(id,account_id,name,split_kind,"
            "default_parts,sheet_tab,created_at) VALUES(?,?,?,?,?,?,?)",
            (bid, account_id, name, split_kind,
             json.dumps(default_parts or []), sheet_tab, _now_iso()))
        self.conn.commit()
        return bid

    def list_beneficiaries(self, account_id: str, active_only: bool = True) -> list[dict]:
        q = "SELECT * FROM beneficiaries WHERE account_id=?"
        if active_only:
            q += " AND active=1"
        rows = self.conn.execute(q + " ORDER BY name", (account_id,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["default_parts"] = json.loads(d.get("default_parts") or "[]")
            result.append(d)
        return result

    def get_beneficiary(self, bid: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM beneficiaries WHERE id=?", (bid,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["default_parts"] = json.loads(d.get("default_parts") or "[]")
        return d

    def custodial_balance(self, account_id: str) -> float:
        """sum(refills) - sum(disbursements) — never hand-edited, always derived."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) bal FROM transactions WHERE account_id=?",
            (account_id,)).fetchone()
        return round(row["bal"], 2)

    def custodial_cycle_dates(self, account_id: str, limit: int = 6) -> list[str]:
        """Distinct disbursement dates for this account, most recent first."""
        rows = self.conn.execute(
            "SELECT DISTINCT date FROM transactions"
            " WHERE account_id=? AND amount<0 ORDER BY date DESC LIMIT ?",
            (account_id, limit)).fetchall()
        return [r["date"] for r in rows]

    def custodial_last_cycle(self, account_id: str) -> list[dict]:
        """Most recent disbursement per beneficiary — the prefill source for
        the nudge, and (via id/screenshot_path) what a shared screenshot
        should link to."""
        rows = self.conn.execute(
            "SELECT t.id, t.beneficiary_id, t.amount, t.date, t.notes, t.screenshot_path"
            " FROM transactions t"
            " WHERE t.account_id=? AND t.beneficiary_id IS NOT NULL"
            " AND t.date = (SELECT MAX(t2.date) FROM transactions t2"
            "               WHERE t2.beneficiary_id=t.beneficiary_id)"
            " ORDER BY t.date DESC", (account_id,)).fetchall()
        return [dict(r) for r in rows]

    # =========================================================================
    # Column maps (per bank, for CSV import)
    # =========================================================================

    def save_column_map(self, bank_name: str, column_map: dict):
        self.conn.execute(
            "INSERT OR REPLACE INTO bank_column_maps(bank_name,column_map)"
            " VALUES(?,?)",
            (bank_name, json.dumps(column_map)))
        self.conn.commit()

    def get_column_map(self, bank_name: str) -> dict | None:
        row = self.conn.execute(
            "SELECT column_map FROM bank_column_maps WHERE bank_name=?",
            (bank_name,)).fetchone()
        if row is None:
            return None
        return json.loads(row["column_map"])

    def list_column_maps(self) -> list[dict]:
        return [
            {"bank_name": r["bank_name"],
             "column_map": json.loads(r["column_map"])}
            for r in self.conn.execute("SELECT * FROM bank_column_maps")
        ]

    # =========================================================================
    # Business entities
    # =========================================================================

    def add_business_entity(self, name: str, pan: str | None = None,
                            gstin: str | None = None,
                            constitution: str = "proprietorship",
                            registration_state: str | None = None,
                            financial_year: str | None = None,
                            tax_regime: str | None = None,
                            holds_depreciable_assets: bool = False,
                            tracking_closeness: str = "loose") -> str:
        if constitution not in VALID_CONSTITUTIONS:
            raise ValueError(f"constitution must be one of {VALID_CONSTITUTIONS}")
        if tracking_closeness not in VALID_TRACKING_CLOSENESS:
            raise ValueError(f"tracking_closeness must be one of {VALID_TRACKING_CLOSENESS}")
        eid = _uuid()
        self.conn.execute(
            "INSERT INTO business_entities(id,name,pan,gstin,constitution,"
            "registration_state,financial_year,tax_regime,holds_depreciable_assets,"
            "tracking_closeness,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (eid, name, pan, gstin, constitution, registration_state,
             financial_year, tax_regime, int(holds_depreciable_assets),
             tracking_closeness, _now_iso()))
        self.conn.commit()
        return eid

    def get_business_entity(self, entity_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM business_entities WHERE id=?", (entity_id,)).fetchone()
        return dict(row) if row else None

    def list_business_entities(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM business_entities ORDER BY created_at").fetchall()]

    def update_business_entity(self, entity_id: str, **kwargs) -> bool:
        allowed = {"name", "pan", "gstin", "constitution", "registration_state",
                   "financial_year", "tax_regime", "holds_depreciable_assets",
                   "tracking_closeness", "jurisdiction"}
        fields = {}
        for k, v in kwargs.items():
            if k not in allowed:
                continue
            if k == "constitution" and v not in VALID_CONSTITUTIONS:
                raise ValueError(f"constitution must be one of {VALID_CONSTITUTIONS}")
            if k == "tracking_closeness" and v not in VALID_TRACKING_CLOSENESS:
                raise ValueError(f"tracking_closeness must be one of {VALID_TRACKING_CLOSENESS}")
            if k == "holds_depreciable_assets":
                v = int(v)
            fields[k] = v
        if not fields:
            return False
        sets = ", ".join(f"{k}=?" for k in fields)
        c = self.conn.execute(
            f"UPDATE business_entities SET {sets} WHERE id=?",
            list(fields.values()) + [entity_id])
        self.conn.commit()
        return c.rowcount > 0

    def delete_business_entity(self, entity_id: str) -> bool:
        self.conn.execute(
            "DELETE FROM compliance_suggestions WHERE business_entity_id=?", (entity_id,))
        self.conn.execute(
            "DELETE FROM ledger_entries WHERE business_entity_id=?", (entity_id,))
        c = self.conn.execute(
            "DELETE FROM business_entities WHERE id=?", (entity_id,))
        self.conn.commit()
        return c.rowcount > 0

    # =========================================================================
    # Ledger entries (business entity Accountant/Auditor)
    # =========================================================================

    def add_ledger_entry(self, business_entity_id: str, date: str, amount: float,
                         source_event_id: str, description: str = "",
                         category: str = "Uncategorized",
                         source_document: str | None = None,
                         confidence: float = 1.0,
                         posted_by: str = "accountant") -> str:
        lid = _uuid()
        self.conn.execute(
            "INSERT INTO ledger_entries(id,business_entity_id,date,amount,"
            "description,category,source_event_id,source_document,confidence,"
            "posted_by,audit_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (lid, business_entity_id, date, amount, description, category,
             source_event_id, source_document, confidence, posted_by,
             "unaudited", _now_iso()))
        self.conn.commit()
        return lid

    def list_ledger_entries(self, business_entity_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM ledger_entries WHERE business_entity_id=?"
            " ORDER BY date DESC", (business_entity_id,)).fetchall()]

    def get_ledger_entry(self, entry_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM ledger_entries WHERE id=?", (entry_id,)).fetchone()
        return dict(row) if row else None

    def update_ledger_entry(self, entry_id: str, **kwargs) -> bool:
        allowed = {"date", "amount", "description", "category", "audit_status", "posted_by"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return False
        sets = ", ".join(f"{k}=?" for k in fields)
        c = self.conn.execute(
            f"UPDATE ledger_entries SET {sets} WHERE id=?",
            list(fields.values()) + [entry_id])
        self.conn.commit()
        return c.rowcount > 0

    def delete_ledger_entry(self, entry_id: str) -> bool:
        c = self.conn.execute("DELETE FROM ledger_entries WHERE id=?", (entry_id,))
        self.conn.commit()
        return c.rowcount > 0

    def set_ledger_audit_status(self, entry_id: str, audit_status: str):
        self.conn.execute(
            "UPDATE ledger_entries SET audit_status=? WHERE id=?",
            (audit_status, entry_id))
        self.conn.commit()

    # =========================================================================
    # Compliance suggestions
    # =========================================================================

    def add_compliance_suggestion(self, business_entity_id: str, ledger_entry_id: str,
                                  source_event_id: str, suggestion_type: str,
                                  reasoning: str, citation: str,
                                  rate_used: str | None = None,
                                  ca_disclaimer: str = "Confirm with your CA before acting on this.",
                                  routed_sensitive: bool = False) -> str:
        sid = _uuid()
        self.conn.execute(
            "INSERT INTO compliance_suggestions(id,business_entity_id,ledger_entry_id,"
            "source_event_id,suggestion_type,reasoning,rate_used,citation,"
            "ca_disclaimer,routed_sensitive,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (sid, business_entity_id, ledger_entry_id, source_event_id,
             suggestion_type, reasoning, rate_used, citation, ca_disclaimer,
             int(routed_sensitive), _now_iso()))
        self.conn.commit()
        return sid

    def list_compliance_suggestions(self, business_entity_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM compliance_suggestions WHERE business_entity_id=?"
            " ORDER BY created_at DESC", (business_entity_id,)).fetchall()]

    def ledger_entries_without_suggestions(self, business_entity_id: str) -> list[dict]:
        """Ledger entries for this entity that have no compliance_suggestions row yet."""
        return [dict(r) for r in self.conn.execute(
            "SELECT l.* FROM ledger_entries l"
            " WHERE l.business_entity_id=?"
            " AND l.id NOT IN (SELECT ledger_entry_id FROM compliance_suggestions"
            "                  WHERE business_entity_id=?)"
            " ORDER BY l.date", (business_entity_id, business_entity_id)).fetchall()]

    # =========================================================================
    # Rate table
    # =========================================================================

    def add_rate(self, rate_type: str, key: str, value: str,
                effective_from: str, effective_to: str | None = None,
                source_note: str = "") -> str:
        rid = _uuid()
        self.conn.execute(
            "INSERT INTO rate_table(id,rate_type,key,value,effective_from,"
            "effective_to,source_note,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (rid, rate_type, key, value, effective_from, effective_to,
             source_note, _now_iso()))
        self.conn.commit()
        return rid

    def rate_exists(self, rate_type: str, key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM rate_table WHERE rate_type=? AND key=? LIMIT 1",
            (rate_type, key)).fetchone()
        return row is not None

    def list_rates(self, rate_type: str | None = None, current_only: bool = True) -> list[dict]:
        q = "SELECT * FROM rate_table WHERE 1=1"
        params: list = []
        if rate_type:
            q += " AND rate_type=?"; params.append(rate_type)
        if current_only:
            q += " AND effective_to IS NULL"
        q += " ORDER BY rate_type, key"
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]

    def update_rate(self, rate_id: str, **kwargs) -> bool:
        allowed = {"value", "effective_from", "effective_to", "source_note"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return False
        fields["updated_at"] = _now_iso()
        sets = ", ".join(f"{k}=?" for k in fields)
        c = self.conn.execute(
            f"UPDATE rate_table SET {sets} WHERE id=?",
            list(fields.values()) + [rate_id])
        self.conn.commit()
        return c.rowcount > 0

    # =========================================================================
    # Analytics
    # =========================================================================

    def monthly_income(self) -> float:
        return sum(_to_monthly_amount(r["amount"], r["recurrence"])
                   for r in self.list_income_sources())

    def this_month_spend(self) -> dict[str, float]:
        start, end = _this_month_range()
        rows = self.conn.execute(
            "SELECT t.category, SUM(t.amount) total FROM transactions t"
            " LEFT JOIN accounts a ON t.account_id = a.id"
            " WHERE t.date>=? AND t.date<=? AND t.amount<0"
            " AND (a.account_type IS NULL OR a.account_type != 'custodial')"
            " GROUP BY t.category",
            (start, end)).fetchall()
        return {r["category"]: abs(r["total"]) for r in rows}

    def this_month_income_txn(self) -> float:
        start, end = _this_month_range()
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) total FROM transactions"
            " WHERE date>=? AND date<=? AND amount>0", (start, end)).fetchone()
        return row["total"]

    def effective_monthly_income(self, tolerance: float = 0.05) -> float:
        """
        This month's real credited amount (any positive transaction, excluding
        custodial accounts — a refill there isn't the user's income) plus any
        manually-entered income source whose expected monthly amount isn't
        already reflected among those transactions — avoids double-counting
        salary that's both entered manually and imported/synced from the bank.
        """
        start, end = _this_month_range()
        rows = self.conn.execute(
            "SELECT t.amount FROM transactions t"
            " LEFT JOIN accounts a ON t.account_id = a.id"
            " WHERE t.date>=? AND t.date<=? AND t.amount>0"
            " AND (a.account_type IS NULL OR a.account_type != 'custodial')",
            (start, end)).fetchall()
        remaining = [r["amount"] for r in rows]
        txn_total = sum(remaining)

        unmatched = 0.0
        for src in self.list_income_sources():
            monthly_amt = _to_monthly_amount(src["amount"], src["recurrence"])
            match_idx = next(
                (i for i, t in enumerate(remaining)
                 if monthly_amt > 0 and abs(t - monthly_amt) / monthly_amt < tolerance),
                None)
            if match_idx is not None:
                remaining.pop(match_idx)
            else:
                unmatched += monthly_amt
        return round(txn_total + unmatched, 2)

    def balance_estimate(self) -> float:
        return (self.effective_monthly_income()
                - sum(self.this_month_spend().values()))

    def budget_status(self) -> list[dict]:
        spend = self.this_month_spend()
        limits = {r["category"]: r["monthly_limit"] for r in self.list_budgets()}
        result = []
        for cat in sorted(set(spend) | set(limits)):
            spent = spend.get(cat, 0.0)
            limit = limits.get(cat)
            result.append({
                "category": cat,
                "spent": round(spent, 2),
                "limit": round(limit, 2) if limit is not None else None,
                "over_budget": bool(limit is not None and spent > limit),
                "headroom": round(limit - spent, 2) if limit is not None else None,
            })
        return result

    def upcoming_bills(self, days: int = 30) -> list[dict]:
        today = _dt.date.today()
        cutoff = (today + _dt.timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM subscriptions"
            " WHERE status='active' AND renewal_date IS NOT NULL AND renewal_date<=?"
            " ORDER BY renewal_date",
            (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def subscription_total_monthly(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(monthly_cost),0) t"
            " FROM subscriptions WHERE status='active'").fetchone()
        return row["t"]

    def overview(self) -> dict:
        return {
            "balance_estimate": round(self.balance_estimate(), 2),
            "monthly_income": round(self.effective_monthly_income(), 2),
            "this_month_spend": {k: round(v, 2) for k, v in self.this_month_spend().items()},
            "budget_status": self.budget_status(),
            "upcoming_bills": self.upcoming_bills(30),
            "subscription_monthly_total": round(self.subscription_total_monthly(), 2),
            "portfolio": self.portfolio_summary(),
        }

    def context_block(self) -> str:
        ov = self.overview()
        lines = [
            f"[Finance Data — {_today()}]",
            f"  Estimated balance this month: ₹{ov['balance_estimate']:,.0f}",
            f"  Monthly income: ₹{ov['monthly_income']:,.0f}",
            f"  Subscriptions (monthly): ₹{ov['subscription_monthly_total']:,.0f}",
        ]
        if ov["this_month_spend"]:
            top = sorted(ov["this_month_spend"].items(), key=lambda x: -x[1])[:5]
            lines.append("  Top spending categories:")
            for cat, amt in top:
                lines.append(f"    {cat}: ₹{amt:,.0f}")
        over = [b["category"] for b in ov["budget_status"] if b["over_budget"]]
        if over:
            lines.append(f"  OVER BUDGET: {', '.join(over)}")
        if ov["upcoming_bills"]:
            lines.append("  Bills due next 30 days:")
            for b in ov["upcoming_bills"][:5]:
                lines.append(f"    {b['name']} on {b['renewal_date']}: ₹{b['monthly_cost']:,.0f}/mo")
        pf = ov["portfolio"]
        if pf["total_value"]:
            sign = "+" if pf["gain_loss"] >= 0 else ""
            lines.append(
                f"  Portfolio: ₹{pf['total_value']:,.0f}"
                f" (P&L: {sign}₹{pf['gain_loss']:,.0f})")
        return "\n".join(lines)

    def close(self):
        self.conn.close()
