"""Commitments engine (CONTEXT_PLAN C3) — deadline-bearing life admin.

Generalizes the obligations pattern to personal commitments: return windows,
warranties, renewals, document expiries. Rows are advisory (like compliance
suggestions) so detection writes them directly — the value is in the deadline
ladder: ≤3 days → high-priority notification, 4–14 days → normal (same ladder
as bills), past due → auto-expired.

Detection is heuristic and local (no LLM):
  return_window — a debit at a merchant with a known return policy opens a
                  window of that many days from the purchase date
  warranty      — electronics/appliance-ish category OR a large debit gets a
                  12-month warranty entry
Everything else (documents, custom deadlines) enters manually via the API.

Table lives in finance.db (rows reference transactions), created lazily like
learned_category_rules (CLAUDE.md quirk 12).
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import uuid

# merchant token → return window in days (Indian marketplaces; conservative)
RETURN_WINDOWS = {
    "amazon": 10, "flipkart": 7, "myntra": 14, "ajio": 15,
    "nykaa": 15, "meesho": 7, "tatacliq": 14, "croma": 7,
    "reliancedigital": 7, "decathlon": 90, "ikea": 60,
}

WARRANTY_CATEGORIES = {"electronics", "appliances", "gadgets", "electronic"}
WARRANTY_MIN_AMOUNT = 10000        # any debit this large probably has one
WARRANTY_DAYS = 365
_SKIP_CATEGORIES = {"transfer", "investment", "custodial disbursement",
                    "rent", "salary", "emi", "loan"}

DUE_SOON_DAYS = 3                  # high-priority rung of the ladder
DUE_UPCOMING_DAYS = 14             # normal rung (warranties/documents only)
LOOKBACK_DAYS = 30                 # how far back detection scans purchases

KINDS = ("return_window", "warranty", "renewal", "document", "custom")


def _today() -> _dt.date:
    return _dt.date.today()


def _merchant_tokens(merchant: str) -> list[str]:
    return [t.lower() for t in re.split(r"[^A-Za-z]+", merchant or "")
            if len(t) >= 4]


class CommitmentEngine:
    def __init__(self, fe):
        """fe: an open FinanceEngine (caller owns/closes it)."""
        self.fe = fe
        self.conn = fe.conn
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS commitments ("
            " id TEXT PRIMARY KEY, kind TEXT NOT NULL,"
            " title TEXT NOT NULL, merchant TEXT DEFAULT '',"
            " amount REAL, ref_txn_id TEXT,"
            " source TEXT DEFAULT 'manual',"
            " start_date TEXT, due_date TEXT NOT NULL,"
            " status TEXT DEFAULT 'open',"
            " notes TEXT DEFAULT '', meta TEXT DEFAULT '{}',"
            " created_at TEXT)")
        self.conn.commit()

    # --- CRUD ---------------------------------------------------------------
    def add(self, kind: str, title: str, due_date: str, merchant: str = "",
            amount: float | None = None, ref_txn_id: str | None = None,
            source: str = "manual", start_date: str | None = None,
            notes: str = "", meta: dict | None = None) -> str:
        cid = uuid.uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO commitments (id,kind,title,merchant,amount,ref_txn_id,"
            " source,start_date,due_date,status,notes,meta,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,'open',?,?,?)",
            (cid, kind, title.strip(), merchant, amount, ref_txn_id, source,
             start_date or _today().isoformat(), due_date, notes,
             json.dumps(meta or {}),
             _dt.datetime.now(_dt.timezone.utc).isoformat()))
        self.conn.commit()
        return cid

    def list(self, status: str | None = "open", limit: int = 200) -> list[dict]:
        q = "SELECT * FROM commitments"
        params: list = []
        if status and status != "all":
            q += " WHERE status=?"
            params.append(status)
        q += " ORDER BY due_date LIMIT ?"
        params.append(limit)
        out = []
        for r in self.conn.execute(q, params).fetchall():
            d = dict(r)
            d["meta"] = json.loads(d.get("meta") or "{}")
            d["days_left"] = (_dt.date.fromisoformat(d["due_date"])
                              - _today()).days
            out.append(d)
        return out

    def update(self, cid: str, **fields) -> bool:
        allowed = {"status", "due_date", "notes", "title"}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed and v is not None:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return False
        vals.append(cid)
        c = self.conn.execute(
            f"UPDATE commitments SET {', '.join(sets)} WHERE id=?", vals)
        self.conn.commit()
        return c.rowcount > 0

    def delete(self, cid: str) -> bool:
        c = self.conn.execute("DELETE FROM commitments WHERE id=?", (cid,))
        self.conn.commit()
        return c.rowcount > 0

    def _exists_for_txn(self, txn_id: str, kind: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM commitments WHERE ref_txn_id=? AND kind=? LIMIT 1",
            (txn_id, kind)).fetchone() is not None

    # --- detection ------------------------------------------------------------
    def detect(self) -> list[str]:
        """Scan recent debits → auto-create return-window/warranty rows.
        Idempotent per (transaction, kind). Returns created ids."""
        since = (_today() - _dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
        created: list[str] = []
        for t in self.fe.list_transactions(limit=2000, since=since):
            amount = t.get("amount") or 0
            if amount >= 0:
                continue
            category = (t.get("category") or "").strip().lower()
            if category in _SKIP_CATEGORIES:
                continue
            merchant = (t.get("merchant") or "").strip()
            purchase = _dt.date.fromisoformat(t["date"][:10])

            # any token may be the marketplace ("MYNTRA DESIGNS" → myntra)
            window = next((RETURN_WINDOWS[tok]
                           for tok in _merchant_tokens(merchant)
                           if tok in RETURN_WINDOWS), None)
            if window:
                due = purchase + _dt.timedelta(days=window)
                if due >= _today() and not self._exists_for_txn(t["id"], "return_window"):
                    created.append(self.add(
                        "return_window",
                        f"Return window: {merchant[:60]} ₹{abs(amount):,.0f}",
                        due.isoformat(), merchant=merchant, amount=abs(amount),
                        ref_txn_id=t["id"], source="auto",
                        start_date=purchase.isoformat(),
                        meta={"window_days": window}))

            warranty = (category in WARRANTY_CATEGORIES
                        or abs(amount) >= WARRANTY_MIN_AMOUNT)
            if warranty and not self._exists_for_txn(t["id"], "warranty"):
                due = purchase + _dt.timedelta(days=WARRANTY_DAYS)
                created.append(self.add(
                    "warranty",
                    f"Warranty: {merchant[:60] or category or 'purchase'} "
                    f"₹{abs(amount):,.0f}",
                    due.isoformat(), merchant=merchant, amount=abs(amount),
                    ref_txn_id=t["id"], source="auto",
                    start_date=purchase.isoformat(),
                    meta={"warranty_days": WARRANTY_DAYS}))
        return created

    def expire_past_due(self) -> int:
        c = self.conn.execute(
            "UPDATE commitments SET status='expired'"
            " WHERE status='open' AND due_date < ?", (_today().isoformat(),))
        self.conn.commit()
        return c.rowcount


# ---------------------------------------------------------------------------
# Job handler
# ---------------------------------------------------------------------------

def commitment_scan(ctx) -> dict:
    """Daily job: detect new commitments, walk the deadline ladder, expire."""
    fe = ctx.open_finance()
    ns = ctx.notify_store()
    try:
        ce = CommitmentEngine(fe)
        created = ce.detect()

        # announce newly detected windows once, quietly
        for cid in created:
            row = next((c for c in ce.list("open") if c["id"] == cid), None)
            if row and not ns.exists_today("commitment_created", cid):
                ns.create(type="commitment_created",
                          title=row["title"],
                          body=(f"Tracked automatically — closes {row['due_date']}"
                                f" ({row['days_left']}d). Mark done/dismissed in"
                                " Commitments if not needed."),
                          priority="normal",
                          related_entity={"entity_type": "commitment",
                                          "id": cid, "kind": row["kind"]})

        # deadline ladder (same rungs as bills: ≤3d high, 4–14d normal)
        notified = 0
        for c in ce.list("open"):
            days = c["days_left"]
            if days < 0:
                continue
            if days <= DUE_SOON_DAYS:
                ref = f"commit_{c['id']}_3d"
                if not ns.exists_today("commitment_due_soon", ref):
                    ns.create(type="commitment_due_soon",
                              title=f"Closes in {days}d: {c['title']}",
                              body=(f"{c['kind'].replace('_', ' ').title()} ends "
                                    f"{c['due_date']}. Act now or mark it done."),
                              priority="high",
                              related_entity={"entity_type": "commitment",
                                              "id": c["id"], "ref": ref})
                    notified += 1
            elif days <= DUE_UPCOMING_DAYS and c["kind"] != "return_window":
                # return windows are short-lived; a 14-day heads-up is noise
                ref = f"commit_{c['id']}_14d"
                if not ns.exists_today("commitment_upcoming", ref):
                    ns.create(type="commitment_upcoming",
                              title=f"Upcoming: {c['title']}",
                              body=f"Due {c['due_date']} ({days}d away).",
                              priority="normal",
                              related_entity={"entity_type": "commitment",
                                              "id": c["id"], "ref": ref})
                    notified += 1

        expired = ce.expire_past_due()
        return {"created": len(created), "notified": notified,
                "expired": expired}
    finally:
        fe.close()
