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


def _month_bounds(d: _dt.date) -> tuple[str, str]:
    start = d.replace(day=1)
    if d.month == 12:
        end = d.replace(year=d.year + 1, month=1, day=1) - _dt.timedelta(days=1)
    else:
        end = d.replace(month=d.month + 1, day=1) - _dt.timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _this_month_range() -> tuple[str, str]:
    return _month_bounds(_dt.date.today())


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
                tracking_only INTEGER NOT NULL DEFAULT 0,
                expected_amount REAL,
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
            CREATE TABLE IF NOT EXISTS suggestion_cache (
                kind         TEXT PRIMARY KEY,
                payload      TEXT NOT NULL,
                computed_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS aml_cases (
                id           TEXT PRIMARY KEY,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                account_id   TEXT,
                status       TEXT NOT NULL DEFAULT 'open',
                typology     TEXT NOT NULL,
                risk_level   TEXT NOT NULL,
                score        REAL DEFAULT 0,
                evidence     TEXT NOT NULL DEFAULT '[]',
                timeline     TEXT NOT NULL DEFAULT '[]',
                explanation  TEXT DEFAULT '',
                sar_draft    TEXT,
                escalated_at TEXT,
                closed_at    TEXT
            );
            CREATE TABLE IF NOT EXISTS credit_scores (
                id                       TEXT PRIMARY KEY,
                computed_at              TEXT NOT NULL,
                score                    INTEGER NOT NULL,
                factors                  TEXT NOT NULL DEFAULT '{}',
                explanation              TEXT DEFAULT '',
                improvement_suggestions  TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS loan_applications (
                id                 TEXT PRIMARY KEY,
                created_at         TEXT NOT NULL,
                updated_at         TEXT NOT NULL,
                loan_type          TEXT NOT NULL,
                jurisdiction       TEXT NOT NULL,
                amount_requested   REAL NOT NULL,
                term_months        INTEGER NOT NULL,
                financing_structure TEXT,
                status             TEXT NOT NULL DEFAULT 'pending',
                approval_id        TEXT,
                credit_score_used  INTEGER,
                recommended_rate   REAL,
                recommended_amount REAL,
                emi                REAL,
                decision           TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS loan_schedules (
                id                   TEXT PRIMARY KEY,
                loan_application_id  TEXT NOT NULL,
                installment_number   INTEGER NOT NULL,
                due_date             TEXT NOT NULL,
                principal            REAL NOT NULL,
                interest             REAL NOT NULL,
                emi                  REAL NOT NULL,
                balance              REAL NOT NULL
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
            CREATE INDEX IF NOT EXISTS idx_aml_status ON aml_cases(status);
            CREATE INDEX IF NOT EXISTS idx_aml_account ON aml_cases(account_id);
            CREATE INDEX IF NOT EXISTS idx_credit_computed ON credit_scores(computed_at);
            CREATE INDEX IF NOT EXISTS idx_loan_status ON loan_applications(status);
            CREATE INDEX IF NOT EXISTS idx_loan_jurisdiction ON loan_applications(jurisdiction);
            CREATE INDEX IF NOT EXISTS idx_loan_schedule_app ON loan_schedules(loan_application_id);
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
        # an optional attached UPI/NEFT confirmation screenshot, and which
        # split part it covers (e.g. Eswari "Personal" vs "MJVR" — one
        # beneficiary paid in two labelled transfers per cycle).
        for col, coltype in (("beneficiary_id", "TEXT"), ("screenshot_path", "TEXT"),
                             ("part", "TEXT")):
            try:
                self.conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} {coltype}")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_txn_beneficiary ON transactions(beneficiary_id)")
        self.conn.commit()
        # Tracking-only beneficiaries: recorded for history/nudges but their
        # rows never count toward the custodial balance (money not actually
        # flowing through this account — e.g. MJVR/VJPN commitment trackers).
        # expected_amount: the static per-cycle commitment (user-declared,
        # beats the median guess) used by the low-balance refill alert.
        for col, coltype in (("tracking_only", "INTEGER NOT NULL DEFAULT 0"),
                             ("expected_amount", "REAL")):
            try:
                self.conn.execute(f"ALTER TABLE beneficiaries ADD COLUMN {col} {coltype}")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
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
        # Fraud Detection Module (Phase 1): a rule-based risk annotation per
        # transaction. fraud_reason_codes is a JSON list; fraud_action is the
        # recommended_action string (allow|require_mfa|hold|escalate|block|
        # manual_review) as of the last review — see amy/finance/fraud_engine.py.
        for col, coltype in (("fraud_score", "REAL"), ("fraud_risk_level", "TEXT"),
                             ("fraud_reason_codes", "TEXT"), ("fraud_action", "TEXT"),
                             ("fraud_scored_at", "TEXT")):
            try:
                self.conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} {coltype}")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists

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

    def get_transaction(self, tid: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM transactions WHERE id=?", (tid,)).fetchone()
        return dict(row) if row else None

    # =========================================================================
    # Fraud Detection Module (Phase 1) — see amy/finance/fraud_engine.py for
    # the scoring logic. These are pure persistence helpers: they never score
    # anything themselves, only save/read what fraud_engine already computed.
    # =========================================================================

    def save_fraud_score(self, transaction_id: str, score: dict) -> bool:
        c = self.conn.execute(
            "UPDATE transactions SET fraud_score=?, fraud_risk_level=?,"
            " fraud_reason_codes=?, fraud_action=?, fraud_scored_at=? WHERE id=?",
            (score.get("score"), score.get("risk_level"),
             json.dumps(score.get("reason_codes") or []),
             score.get("recommended_action"), _now_iso(), transaction_id))
        self.conn.commit()
        return c.rowcount > 0

    def get_fraud_score(self, transaction_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT fraud_score, fraud_risk_level, fraud_reason_codes,"
            " fraud_action, fraud_scored_at FROM transactions WHERE id=?",
            (transaction_id,)).fetchone()
        if row is None or row["fraud_scored_at"] is None:
            return None
        d = dict(row)
        d["fraud_reason_codes"] = json.loads(d["fraud_reason_codes"] or "[]")
        return d

    def list_flagged_transactions(self, risk_level: str | None = None,
                                  limit: int = 100) -> list[dict]:
        q = ("SELECT * FROM transactions WHERE fraud_scored_at IS NOT NULL"
             " AND fraud_risk_level IS NOT NULL AND fraud_risk_level != 'LOW'")
        params: list = []
        if risk_level:
            q += " AND fraud_risk_level=?"
            params.append(risk_level.upper())
        q += " ORDER BY fraud_scored_at DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in self.conn.execute(q, params).fetchall()]
        for r in rows:
            r["fraud_reason_codes"] = json.loads(r["fraud_reason_codes"] or "[]")
        return rows

    # =========================================================================
    # AML Monitoring Module (Phase 2) — see amy/finance/aml_engine.py for the
    # typology detectors. Pure persistence helpers, same convention as the
    # fraud-score helpers above: they never detect anything themselves.
    # =========================================================================

    def _row_to_aml_case(self, row) -> dict:
        d = dict(row)
        d["evidence"] = json.loads(d["evidence"] or "[]")
        d["timeline"] = json.loads(d["timeline"] or "[]")
        return d

    def create_aml_case(self, account_id: str | None, typology: str, risk_level: str,
                        score: float, evidence: list[str], timeline: list[dict],
                        explanation: str) -> str:
        cid = _uuid()
        now = _now_iso()
        self.conn.execute(
            "INSERT INTO aml_cases(id,created_at,updated_at,account_id,status,"
            "typology,risk_level,score,evidence,timeline,explanation)"
            " VALUES(?,?,?,?,'open',?,?,?,?,?,?)",
            (cid, now, now, account_id, typology, risk_level, score,
             json.dumps(evidence), json.dumps(timeline), explanation))
        self.conn.commit()
        return cid

    def get_aml_case(self, case_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM aml_cases WHERE id=?", (case_id,)).fetchone()
        return self._row_to_aml_case(row) if row else None

    def list_aml_cases(self, status: str | None = None, account_id: str | None = None,
                       typology: str | None = None, limit: int = 100) -> list[dict]:
        q = "SELECT * FROM aml_cases WHERE 1=1"
        params: list = []
        if status:
            q += " AND status=?"; params.append(status)
        if account_id:
            q += " AND account_id=?"; params.append(account_id)
        if typology:
            q += " AND typology=?"; params.append(typology)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [self._row_to_aml_case(r) for r in self.conn.execute(q, params).fetchall()]

    def find_open_aml_case(self, account_id: str | None, typology: str,
                           evidence: list[str]) -> dict | None:
        """Dedup check: an existing open/investigating case for the same
        account+typology whose evidence overlaps this candidate's — used so
        re-scanning doesn't open a duplicate case for the same underlying
        transactions. Escalated/closed cases don't block a fresh one (a
        genuinely new cluster of transactions after a case was closed is a
        new case, same spirit as approvals' dedup only covering open rows)."""
        candidates = self.list_aml_cases(account_id=account_id, typology=typology)
        ev_set = set(evidence)
        for c in candidates:
            if c["status"] not in ("open", "investigating"):
                continue
            if ev_set & set(c["evidence"]):
                return c
        return None

    def update_aml_case_status(self, case_id: str, status: str) -> bool:
        now = _now_iso()
        extra = ""
        params: list = [status, now]
        if status == "escalated":
            extra = ", escalated_at=?"
            params.append(now)
        elif status == "closed":
            extra = ", closed_at=?"
            params.append(now)
        params.append(case_id)
        c = self.conn.execute(
            f"UPDATE aml_cases SET status=?, updated_at=?{extra} WHERE id=?", params)
        self.conn.commit()
        return c.rowcount > 0

    def append_aml_case_timeline(self, case_id: str, entry: dict) -> bool:
        case = self.get_aml_case(case_id)
        if case is None:
            return False
        timeline = case["timeline"] + [entry]
        c = self.conn.execute(
            "UPDATE aml_cases SET timeline=?, updated_at=? WHERE id=?",
            (json.dumps(timeline), _now_iso(), case_id))
        self.conn.commit()
        return c.rowcount > 0

    def save_aml_sar_draft(self, case_id: str, sar_draft: str) -> bool:
        c = self.conn.execute(
            "UPDATE aml_cases SET sar_draft=?, updated_at=? WHERE id=?",
            (sar_draft, _now_iso(), case_id))
        self.conn.commit()
        return c.rowcount > 0

    # =========================================================================
    # Amy Credit Score (Phase 3) — see amy/finance/credit_engine.py for the
    # scoring logic. Pure persistence helpers, same convention as the fraud/
    # AML helpers above: they never compute anything themselves. One row per
    # computation (a time series for /api/credit/history), not upserted.
    # =========================================================================

    def _row_to_credit_score(self, row) -> dict:
        d = dict(row)
        d["factors"] = json.loads(d["factors"] or "{}")
        d["improvement_suggestions"] = json.loads(d["improvement_suggestions"] or "[]")
        return d

    def save_credit_score(self, score: int, factors: dict, explanation: str,
                          improvement_suggestions: list[str]) -> str:
        cid = _uuid()
        self.conn.execute(
            "INSERT INTO credit_scores(id,computed_at,score,factors,explanation,"
            "improvement_suggestions) VALUES(?,?,?,?,?,?)",
            (cid, _now_iso(), score, json.dumps(factors), explanation,
             json.dumps(improvement_suggestions)))
        self.conn.commit()
        return cid

    def get_latest_credit_score(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM credit_scores ORDER BY computed_at DESC LIMIT 1").fetchone()
        return self._row_to_credit_score(row) if row else None

    def list_credit_score_history(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM credit_scores ORDER BY computed_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [self._row_to_credit_score(r) for r in rows]

    # =========================================================================
    # Loan Underwriting Module (Phase 5) — see amy/finance/loan_engine.py for
    # the calculators/underwriting logic. Pure persistence helpers, same
    # convention as the fraud/AML/credit helpers above.
    # =========================================================================

    def _row_to_loan_application(self, row) -> dict:
        d = dict(row)
        d["decision"] = json.loads(d["decision"] or "{}")
        return d

    def create_loan_application(self, loan_type: str, jurisdiction: str,
                                amount_requested: float, term_months: int,
                                financing_structure: str | None,
                                decision: dict) -> str:
        aid = _uuid()
        now = _now_iso()
        self.conn.execute(
            "INSERT INTO loan_applications(id,created_at,updated_at,loan_type,"
            "jurisdiction,amount_requested,term_months,financing_structure,"
            "status,credit_score_used,recommended_rate,recommended_amount,emi,"
            "decision) VALUES(?,?,?,?,?,?,?,?,'pending',?,?,?,?,?)",
            (aid, now, now, loan_type, jurisdiction, amount_requested, term_months,
             financing_structure, decision.get("explanation", {}).get("credit_score_used"),
             decision.get("recommended_rate"), decision.get("recommended_amount"),
             decision.get("emi"), json.dumps(decision)))
        self.conn.commit()
        return aid

    def get_loan_application(self, application_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM loan_applications WHERE id=?", (application_id,)).fetchone()
        return self._row_to_loan_application(row) if row else None

    def list_loan_applications(self, status: str | None = None,
                               jurisdiction: str | None = None,
                               limit: int = 100) -> list[dict]:
        q = "SELECT * FROM loan_applications WHERE 1=1"
        params: list = []
        if status:
            q += " AND status=?"; params.append(status)
        if jurisdiction:
            q += " AND jurisdiction=?"; params.append(jurisdiction)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [self._row_to_loan_application(r)
               for r in self.conn.execute(q, params).fetchall()]

    def update_loan_application_status(self, application_id: str, status: str) -> bool:
        c = self.conn.execute(
            "UPDATE loan_applications SET status=?, updated_at=? WHERE id=?",
            (status, _now_iso(), application_id))
        self.conn.commit()
        return c.rowcount > 0

    def set_loan_application_approval_id(self, application_id: str, approval_id: str) -> bool:
        c = self.conn.execute(
            "UPDATE loan_applications SET approval_id=?, updated_at=? WHERE id=?",
            (approval_id, _now_iso(), application_id))
        self.conn.commit()
        return c.rowcount > 0

    def save_loan_schedule(self, application_id: str, rows: list[dict]) -> None:
        self.conn.execute("DELETE FROM loan_schedules WHERE loan_application_id=?",
                          (application_id,))
        for r in rows:
            self.conn.execute(
                "INSERT INTO loan_schedules(id,loan_application_id,installment_number,"
                "due_date,principal,interest,emi,balance) VALUES(?,?,?,?,?,?,?,?)",
                (_uuid(), application_id, r["installment_number"], r["due_date"],
                 r["principal"], r["interest"], r["emi"], r["balance"]))
        self.conn.commit()

    def get_loan_schedule(self, application_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM loan_schedules WHERE loan_application_id=?"
            " ORDER BY installment_number", (application_id,)).fetchall()
        return [dict(r) for r in rows]

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
        """Dedup guard (found live: repeat Gmail-sync detections of the
        same recurring charge each created a fresh row — 'YouTube Premium'
        x3 at the same cost/renewal date): an existing ACTIVE subscription
        whose name matches case/whitespace-insensitively is updated in
        place (cost/renewal_date refreshed) instead of duplicated. A
        cancelled-then-resubscribed sub (status != 'active') is not
        matched, so a genuine new subscription after cancellation still
        inserts a fresh row."""
        norm = " ".join(name.split()).lower()
        active = self.conn.execute(
            "SELECT id, name FROM subscriptions WHERE status='active'").fetchall()
        existing = next((r for r in active
                         if " ".join(r["name"].split()).lower() == norm), None)
        if existing:
            sid = existing["id"]
            self.conn.execute(
                "UPDATE subscriptions SET monthly_cost=?, annual_cost=?,"
                " renewal_date=COALESCE(?, renewal_date) WHERE id=?",
                (monthly_cost, annual_cost, renewal_date, sid))
            self.conn.commit()
            return sid
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
    # Suggestion cache (budget/subscription/investment/income) — computed
    # once on import, read instantly on tab open. See finance/suggestion_
    # cache.py for the recompute-and-store entry point.
    # =========================================================================

    def get_cached_suggestions(self, kind: str) -> dict | None:
        row = self.conn.execute(
            "SELECT payload, computed_at FROM suggestion_cache WHERE kind=?",
            (kind,)).fetchone()
        if row is None:
            return None
        return {"computed_at": row["computed_at"], **json.loads(row["payload"])}

    def set_cached_suggestions(self, kind: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO suggestion_cache(kind,payload,computed_at) VALUES(?,?,?)"
            " ON CONFLICT(kind) DO UPDATE SET payload=excluded.payload,"
            " computed_at=excluded.computed_at",
            (kind, json.dumps(payload), _now_iso()))
        self.conn.commit()

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
        """sum(refills) - sum(disbursements) — never hand-edited, always derived.
        Rows linked to tracking-only beneficiaries are excluded: they're
        records the user keeps in the same sheet, not money moving through
        this account."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) bal FROM transactions"
            " WHERE account_id=? AND (beneficiary_id IS NULL OR beneficiary_id NOT IN"
            "  (SELECT id FROM beneficiaries WHERE tracking_only=1))",
            (account_id,)).fetchone()
        return round(row["bal"], 2)

    def update_beneficiary(self, bid: str, **kwargs) -> bool:
        allowed = {"name", "sheet_tab", "active", "tracking_only", "split_kind",
                   "expected_amount", "default_parts"}
        if "default_parts" in kwargs and kwargs["default_parts"] is not None:
            kwargs["default_parts"] = json.dumps(kwargs["default_parts"])
        fields = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        if not fields:
            return False
        sets = ", ".join(f"{k}=?" for k in fields)
        c = self.conn.execute(
            f"UPDATE beneficiaries SET {sets} WHERE id=?",
            list(fields.values()) + [bid])
        self.conn.commit()
        return c.rowcount > 0

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

    def latest_month_range(self) -> tuple[str, str]:
        """Month containing the most recent transaction, falling back to the
        real calendar month when there's no transaction data yet. Used by the
        dashboard overview so an imported historical statement (e.g. a bank
        export that ends months before "today") doesn't render as all-zero
        just because none of its rows fall in the real current month —
        Overview should reflect the most recent period that actually has
        data, whatever month/year that is. Other callers (obligations/zakat,
        values screening, budget suggestions, closers) keep using the real
        calendar month via `_this_month_range()` — this is dashboard-only."""
        row = self.conn.execute("SELECT MAX(date) d FROM transactions").fetchone()
        latest = row["d"] if row else None
        if not latest:
            return _this_month_range()
        return _month_bounds(_dt.date.fromisoformat(latest[:10]))

    def this_month_spend(self, date_range: tuple[str, str] | None = None) -> dict[str, float]:
        start, end = date_range or _this_month_range()
        rows = self.conn.execute(
            "SELECT t.category, SUM(t.amount) total FROM transactions t"
            " LEFT JOIN accounts a ON t.account_id = a.id"
            " WHERE t.date>=? AND t.date<=? AND t.amount<0"
            " AND (a.account_type IS NULL OR a.account_type != 'custodial')"
            " GROUP BY t.category",
            (start, end)).fetchall()
        return {r["category"]: abs(r["total"]) for r in rows}

    def this_month_income_txn(self, date_range: tuple[str, str] | None = None) -> float:
        start, end = date_range or _this_month_range()
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) total FROM transactions"
            " WHERE date>=? AND date<=? AND amount>0", (start, end)).fetchone()
        return row["total"]

    def effective_monthly_income(self, tolerance: float = 0.05,
                                  date_range: tuple[str, str] | None = None) -> float:
        """
        This month's real credited amount (any positive transaction, excluding
        custodial accounts — a refill there isn't the user's income) plus any
        manually-entered income source whose expected monthly amount isn't
        already reflected among those transactions — avoids double-counting
        salary that's both entered manually and imported/synced from the bank.
        """
        start, end = date_range or _this_month_range()
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
        total = round(txn_total + unmatched, 2)
        if total > 0:
            return total
        # Salary typically lands near month-end, so early in the month the
        # current-month credit total is 0 and everything downstream (budget
        # suggestions, afford checks) would wrongly see "no income" — fall
        # back to what recent full months actually credited.
        return self.detected_monthly_income()

    def detected_monthly_income(self, months: int = 3) -> float:
        """Median of total credits per calendar month over the last `months`
        full months (current month excluded; custodial accounts excluded).
        The estimate used before this month's salary actually arrives."""
        import datetime as _dtm
        cur = _dtm.date.today().replace(day=1)
        yms = []
        for _ in range(months):
            cur = (cur - _dtm.timedelta(days=1)).replace(day=1)
            yms.append(cur.strftime("%Y-%m"))
        marks = ",".join("?" * len(yms))
        totals = sorted(r["total"] for r in self.conn.execute(
            f"SELECT SUBSTR(t.date,1,7) ym, COALESCE(SUM(t.amount),0) total"
            f" FROM transactions t LEFT JOIN accounts a ON t.account_id=a.id"
            f" WHERE t.amount>0"
            f" AND (a.account_type IS NULL OR a.account_type!='custodial')"
            f" AND SUBSTR(t.date,1,7) IN ({marks}) GROUP BY ym", yms).fetchall())
        if not totals:
            return 0.0
        mid = len(totals) // 2
        med = totals[mid] if len(totals) % 2 else (totals[mid - 1] + totals[mid]) / 2
        return round(med, 2)

    def balance_estimate(self, date_range: tuple[str, str] | None = None) -> float:
        return (self.effective_monthly_income(date_range=date_range)
                - sum(self.this_month_spend(date_range).values()))

    def budget_status(self, date_range: tuple[str, str] | None = None) -> list[dict]:
        spend = self.this_month_spend(date_range)
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
        date_range = self.latest_month_range()
        return {
            "period": date_range[0][:7],
            "balance_estimate": round(self.balance_estimate(date_range), 2),
            "monthly_income": round(self.effective_monthly_income(date_range=date_range), 2),
            "this_month_spend": {k: round(v, 2) for k, v in self.this_month_spend(date_range).items()},
            "budget_status": self.budget_status(date_range),
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
