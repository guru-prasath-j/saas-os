"""Values screening engine (Phase R7A-1).

A ValuesProfile is a DATA object — a list of rules over merchant text,
categories, amounts, and financing types. There is no religion/company/
country branch anywhere in this code: `interest_free_finance`, `esg_basic`,
and `budget_discipline` are just three preset rule-lists in presets.json,
and users can edit their own copies freely.

Shape mirrors the categorizer: deterministic rule pre-filter first; a rule
may set "llm_confirm": true to get an optional LLM sanity check (routed
through LLMRouter with the same sensitivity rules — descriptions matching
tax-ID patterns go local-only).

Storage:
  values_profiles  (finance.db)  — per-user, editable profiles
  screening_flags  (collab.db)   — flags with reasoning; the audit export
                                   already includes this table
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import uuid
from pathlib import Path

_PRESETS_PATH = Path(__file__).parent / "presets.json"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Presets (data)
# ---------------------------------------------------------------------------

def list_presets() -> list[dict]:
    return json.loads(_PRESETS_PATH.read_text(encoding="utf-8"))["presets"]


def get_preset(preset_id: str) -> dict | None:
    return next((p for p in list_presets() if p["id"] == preset_id), None)


# ---------------------------------------------------------------------------
# Per-user profiles (finance.db)
# ---------------------------------------------------------------------------

def _ensure_profiles(fe):
    fe.conn.execute(
        "CREATE TABLE IF NOT EXISTS values_profiles ("
        " id TEXT PRIMARY KEY, preset_id TEXT, name TEXT NOT NULL,"
        " rules TEXT NOT NULL, enabled INTEGER DEFAULT 1, created_at TEXT)")
    fe.conn.commit()


def enable_profile(fe, preset_id: str | None = None, name: str | None = None,
                   rules: list[dict] | None = None) -> str:
    """Copy a preset into the user's editable profiles, or create a custom
    profile from explicit rules."""
    _ensure_profiles(fe)
    if preset_id:
        preset = get_preset(preset_id)
        if preset is None:
            raise ValueError(f"unknown values preset {preset_id!r}")
        name = name or preset["name"]
        rules = rules or preset["rules"]
        row = fe.conn.execute(
            "SELECT id FROM values_profiles WHERE preset_id=?",
            (preset_id,)).fetchone()
        if row:   # re-enabling an existing copy
            fe.conn.execute(
                "UPDATE values_profiles SET enabled=1 WHERE id=?", (row["id"],))
            fe.conn.commit()
            return row["id"]
    if not name or rules is None:
        raise ValueError("custom profiles need name and rules")
    pid = uuid.uuid4().hex[:12]
    fe.conn.execute(
        "INSERT INTO values_profiles(id,preset_id,name,rules,enabled,created_at)"
        " VALUES(?,?,?,?,1,?)",
        (pid, preset_id, name, json.dumps(rules), _now()))
    fe.conn.commit()
    return pid


def update_profile(fe, pid: str, enabled: bool | None = None,
                   rules: list[dict] | None = None) -> bool:
    _ensure_profiles(fe)
    if enabled is not None:
        fe.conn.execute("UPDATE values_profiles SET enabled=? WHERE id=?",
                        (1 if enabled else 0, pid))
    if rules is not None:
        fe.conn.execute("UPDATE values_profiles SET rules=? WHERE id=?",
                        (json.dumps(rules), pid))
    fe.conn.commit()
    return fe.conn.execute("SELECT 1 FROM values_profiles WHERE id=?",
                           (pid,)).fetchone() is not None


def list_profiles(fe, enabled_only: bool = False) -> list[dict]:
    _ensure_profiles(fe)
    q = "SELECT * FROM values_profiles"
    if enabled_only:
        q += " WHERE enabled=1"
    out = []
    for r in fe.conn.execute(q).fetchall():
        d = dict(r)
        d["rules"] = json.loads(d["rules"] or "[]")
        d["enabled"] = bool(d["enabled"])
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Screening flags (collab.db — joined by the audit export)
# ---------------------------------------------------------------------------

def _ensure_flags(collab_conn):
    collab_conn.execute(
        "CREATE TABLE IF NOT EXISTS screening_flags ("
        " id TEXT PRIMARY KEY, created_at TEXT, transaction_id TEXT,"
        " profile_id TEXT, profile_name TEXT, rule_kind TEXT,"
        " severity TEXT, reasoning TEXT, status TEXT DEFAULT 'open')")
    collab_conn.execute(
        "CREATE TABLE IF NOT EXISTS screened_txns ("
        " transaction_id TEXT PRIMARY KEY, ts TEXT)")
    collab_conn.commit()


def list_flags(collab_conn, status: str | None = "open",
               limit: int = 100) -> list[dict]:
    _ensure_flags(collab_conn)
    if status:
        rows = collab_conn.execute(
            "SELECT * FROM screening_flags WHERE status=?"
            " ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
    else:
        rows = collab_conn.execute(
            "SELECT * FROM screening_flags ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def set_flag_status(collab_conn, fid: str, status: str) -> bool:
    _ensure_flags(collab_conn)
    c = collab_conn.execute("UPDATE screening_flags SET status=? WHERE id=?",
                            (status, fid))
    collab_conn.commit()
    return c.rowcount > 0


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------

def _rule_matches(rule: dict, txn: dict, monthly_income: float) -> str | None:
    """Return the flag reason when the rule matches this transaction."""
    kind = rule.get("kind")
    text = f"{txn.get('merchant', '')} {txn.get('notes', '')}"

    if kind == "description_pattern":
        try:
            if re.search(rule.get("pattern", ""), text, re.IGNORECASE):
                return rule.get("reason", "matched description pattern")
        except re.error:
            return None
        return None

    if kind == "category":
        if txn.get("category") in (rule.get("categories") or []):
            return rule.get("reason", "flagged category")
        return None

    if kind == "amount_share_of_income":
        amt = float(txn.get("amount") or 0)
        if amt >= 0 or monthly_income <= 0:
            return None
        if txn.get("category") in (rule.get("exclude_categories") or []):
            return None
        share = abs(amt) / monthly_income
        if share > float(rule.get("max_share") or 1.0):
            return (rule.get("reason", "large purchase")
                    + f" ({share:.0%} of monthly income)")
        return None

    # financing_type rules are evaluated by the afford-check (R7A-4),
    # not against individual transactions.
    return None


def screen_transactions(fe, txns: list[dict], profiles: list[dict],
                        llm=None) -> list[dict]:
    """Rule pre-filter (+ optional LLM confirm) over transactions.
    Returns flag dicts (not yet persisted)."""
    monthly_income = 0.0
    try:
        monthly_income = float(fe.effective_monthly_income())
    except Exception:
        pass
    flags = []
    for txn in txns:
        for prof in profiles:
            for rule in prof["rules"]:
                reason = _rule_matches(rule, txn, monthly_income)
                if not reason:
                    continue
                reasoning = (f"'{(txn.get('merchant') or '')[:60]}' "
                             f"({txn.get('date')}, {txn.get('amount')}): {reason} "
                             f"[profile: {prof['name']}]")
                if rule.get("llm_confirm") and llm is not None:
                    if not _llm_confirm(llm, txn, rule, reasoning):
                        continue
                flags.append({
                    "transaction_id": txn.get("id"),
                    "profile_id": prof["id"],
                    "profile_name": prof["name"],
                    "rule_kind": rule.get("kind"),
                    "severity": rule.get("severity", "normal"),
                    "reasoning": reasoning,
                })
                break   # one flag per (txn, profile) is enough
    return flags


def _llm_confirm(llm, txn: dict, rule: dict, reasoning: str) -> bool:
    """Optional LLM sanity check. Sensitivity rules stay intact: descriptions
    matching tax-ID patterns go through the local-only path."""
    try:
        from ..finance.business.sensitivity import is_sensitive
        sensitive = is_sensitive(txn.get("merchant"), txn.get("notes"))
        raw, _ = llm.generate(
            "Answer with exactly YES or NO. Does this transaction genuinely "
            "violate the stated rule? Be strict — only YES when clear.",
            f"Rule: {rule.get('reason')}\nTransaction: {reasoning}",
            sensitive=sensitive)
        return "YES" in (raw or "").upper()
    except Exception:
        return True   # LLM unavailable → keep the deterministic flag


def persist_flags(collab_conn, flags: list[dict]) -> int:
    """Insert flags, skipping (transaction_id, profile_id) pairs already
    flagged. Returns count inserted."""
    _ensure_flags(collab_conn)
    n = 0
    for f in flags:
        dup = collab_conn.execute(
            "SELECT 1 FROM screening_flags WHERE transaction_id=? AND profile_id=?",
            (f["transaction_id"], f["profile_id"])).fetchone()
        if dup:
            continue
        collab_conn.execute(
            "INSERT INTO screening_flags(id,created_at,transaction_id,profile_id,"
            " profile_name,rule_kind,severity,reasoning)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex[:12], _now(), f["transaction_id"], f["profile_id"],
             f["profile_name"], f["rule_kind"], f["severity"], f["reasoning"]))
        n += 1
    collab_conn.commit()
    return n


def unscreened_transactions(fe, collab_conn, since_days: int = 90,
                            limit: int = 500) -> list[dict]:
    _ensure_flags(collab_conn)
    since = (_dt.date.today() - _dt.timedelta(days=since_days)).isoformat()
    txns = fe.list_transactions(limit=limit, since=since)
    seen = {r["transaction_id"] for r in collab_conn.execute(
        "SELECT transaction_id FROM screened_txns").fetchall()}
    return [t for t in txns if t["id"] not in seen]


def mark_screened(collab_conn, txn_ids: list[str]) -> None:
    _ensure_flags(collab_conn)
    now = _now()
    collab_conn.executemany(
        "INSERT OR IGNORE INTO screened_txns(transaction_id, ts) VALUES(?,?)",
        [(tid, now) for tid in txn_ids])
    collab_conn.commit()
