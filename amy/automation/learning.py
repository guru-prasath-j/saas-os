"""Learning categorizer — manual corrections become permanent rules.

When the user fixes a transaction's category, we distill the merchant string
into a stable pattern and save it in finance.db (learned_category_rules).
Learned rules are applied BEFORE the static rule table in the auto-categorize
job, so the system converges toward zero repeat corrections.
"""
from __future__ import annotations

import datetime as _dt
import re
import uuid

_STOPWORDS = {
    "upi", "neft", "imps", "rtgs", "pos", "ach", "payment", "payments",
    "paid", "txn", "transfer", "debit", "credit", "card", "bank", "india",
    "the", "and", "from", "with",
    # corporate boilerplate — never distinguishes a merchant
    "ltd", "pvt", "limited", "private", "company", "corp", "inc",
    "services", "service", "solutions", "technologies", "enterprises",
}


def _ensure_table(fe):
    fe.conn.execute(
        "CREATE TABLE IF NOT EXISTS learned_category_rules ("
        " id TEXT PRIMARY KEY, pattern TEXT UNIQUE, category TEXT,"
        " created_at TEXT, hits INTEGER DEFAULT 0)")
    fe.conn.commit()


def _pattern_from_merchant(merchant: str) -> str | None:
    """Distill a merchant/narration string into a matchable token.

    Picks the longest alphabetic token (≥4 chars) that isn't banking
    boilerplate — 'UPI-SWIGGY LIMITED-...' → 'swiggy'... falls back to the
    two longest tokens joined, or None if nothing usable."""
    tokens = [t.lower() for t in re.split(r"[^A-Za-z]+", merchant or "") if len(t) >= 4]
    tokens = [t for t in tokens if t not in _STOPWORDS]
    if not tokens:
        return None
    return max(tokens, key=len)


def learn_from_correction(fe, merchant: str, category: str) -> str | None:
    """Store a rule from a manual category fix. Returns the pattern saved."""
    if not category or category == "Uncategorized":
        return None
    pattern = _pattern_from_merchant(merchant)
    if not pattern:
        return None
    _ensure_table(fe)
    fe.conn.execute(
        "INSERT INTO learned_category_rules(id,pattern,category,created_at)"
        " VALUES(?,?,?,?)"
        " ON CONFLICT(pattern) DO UPDATE SET category=excluded.category",
        (uuid.uuid4().hex[:12], pattern, category,
         _dt.datetime.now(_dt.timezone.utc).isoformat()))
    fe.conn.commit()
    return pattern


def apply_learned_rules(fe) -> int:
    """Apply every learned rule to still-Uncategorized transactions.
    Returns number of transactions updated."""
    _ensure_table(fe)
    rules = fe.conn.execute(
        "SELECT id, pattern, category FROM learned_category_rules").fetchall()
    updated = 0
    for r in rules:
        c = fe.conn.execute(
            "UPDATE transactions SET category=?"
            " WHERE (category IS NULL OR category='Uncategorized')"
            "   AND lower(merchant) LIKE ?",
            (r["category"], f"%{r['pattern']}%"))
        if c.rowcount:
            updated += c.rowcount
            fe.conn.execute(
                "UPDATE learned_category_rules SET hits=hits+? WHERE id=?",
                (c.rowcount, r["id"]))
    fe.conn.commit()
    return updated


def list_rules(fe) -> list[dict]:
    _ensure_table(fe)
    return [dict(r) for r in fe.conn.execute(
        "SELECT * FROM learned_category_rules ORDER BY hits DESC")]
