"""AI helpers for custodial accounts — screenshot parsing, Gmail-debit
matching, smart prefill, anomaly pre-checks, cycle-close narrative, and the
chat context block.

Privacy: custodial data is in the sensitive class. Every LLM call in this
module passes sensitive=True (LLMRouter routes that to local Ollama only,
falling back to templates — never a cloud API). Deterministic regex/stats do
the real work; the LLM only rescues unparseable text or phrases a summary.
"""
from __future__ import annotations

import datetime as _dt
import difflib
import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import FinanceEngine

# ---------------------------------------------------------------------------
# Beneficiary fuzzy matching
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> list[str]:
    return _WORD_RE.findall((s or "").lower())


def match_beneficiary(beneficiaries: list[dict], text: str) -> tuple[dict | None, float]:
    """Best beneficiary for a free-text receiver/merchant string.
    Token containment first (handles 'Eswari (personal UPI)' vs
    'UPI/eswari@okaxis/...'), then difflib as a tie-breaker."""
    text_toks = set(_tokens(text))
    if not text_toks:
        return None, 0.0
    best, best_score = None, 0.0
    for b in beneficiaries:
        name_toks = [t for t in _tokens(b["name"]) if len(t) > 2]
        if not name_toks:
            continue
        contained = sum(1 for t in name_toks if any(t in tt or tt in t for tt in text_toks))
        score = contained / len(name_toks)
        if score < 1.0:  # tie-break/boost with sequence similarity
            ratio = difflib.SequenceMatcher(
                None, b["name"].lower(), (text or "").lower()).ratio()
            score = max(score, ratio)
        if score > best_score:
            best, best_score = b, score
    if best_score >= 0.55:
        return best, round(best_score, 2)
    return None, round(best_score, 2)


# ---------------------------------------------------------------------------
# Transfer-screenshot OCR text parsing (regex first, local LLM fallback)
# ---------------------------------------------------------------------------

_AMT_RE = re.compile(r"(?:₹|rs\.?\s*|inr\s*)\s*([\d,]+(?:\.\d{1,2})?)", re.I)
_BARE_AMT_RE = re.compile(r"(?<![\d.])(\d{3,7}(?:\.\d{1,2})?)(?![\d.])")
_TO_RE = re.compile(
    r"(?:paid to|to[:\s]|sent to|transferred to|beneficiary[:\s])\s*([A-Za-z][A-Za-z .]{2,40})", re.I)
_REF_RE = re.compile(r"(?:upi ref(?:erence)?(?:\s*no)?|utr|txn id|transaction id)[.:\s]*([A-Za-z0-9]+)", re.I)
_DATE_PATTERNS = (
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), "ymd"),
    (re.compile(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})"), "dmy"),
    (re.compile(r"(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[,\s]+(\d{4})", re.I), "dMy"),
)
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def parse_transfer_text(ocr: str) -> dict:
    """Deterministic extraction of {amount, date, mode, ref, receiver} from
    UPI/NEFT screenshot OCR text. Missing fields come back None/''."""
    out = {"amount": None, "date": None, "mode": None, "ref": "", "receiver": ""}
    text = ocr or ""

    m = _AMT_RE.search(text)
    if m:
        out["amount"] = float(m.group(1).replace(",", ""))
    else:
        # bare number fallback: biggest 3-7 digit figure (avoids dates/refs by size cap)
        nums = [float(x) for x in _BARE_AMT_RE.findall(text)]
        nums = [n for n in nums if 10 <= n <= 5_000_000]
        if nums:
            out["amount"] = max(nums)

    for pat, kind in _DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        try:
            if kind == "ymd":
                d = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            elif kind == "dmy":
                d = _dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            else:
                d = _dt.date(int(m.group(3)), _MONTHS[m.group(2).lower()[:3]], int(m.group(1)))
            out["date"] = d.isoformat()
            break
        except (ValueError, KeyError):
            continue

    low = text.lower()
    if "upi" in low or "@" in text:
        out["mode"] = "UPI"
    elif "neft" in low:
        out["mode"] = "NEFT"
    elif "imps" in low or "transfer" in low:
        out["mode"] = "Account Transfer"

    m = _REF_RE.search(text)
    if m:
        out["ref"] = m.group(1)
    m = _TO_RE.search(text)
    if m:
        out["receiver"] = m.group(1).strip()
    return out


def llm_parse_transfer(llm, ocr: str) -> dict:
    """Local-only LLM rescue when regex found no amount. Returns the same
    shape as parse_transfer_text; empty dict fields on any failure."""
    out = {"amount": None, "date": None, "mode": None, "ref": "", "receiver": ""}
    try:
        raw, _model = llm.generate(
            "You extract payment details from OCR text of an Indian bank/UPI "
            "transfer screenshot. Return STRICT JSON only: "
            '{"amount": <number or null>, "date": "<YYYY-MM-DD or null>", '
            '"mode": "<UPI|NEFT|Account Transfer or null>", '
            '"ref": "<reference id or empty>", "receiver": "<payee name or empty>"}',
            ocr[:4000], sensitive=True)
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        if isinstance(data.get("amount"), (int, float)) and data["amount"] > 0:
            out["amount"] = float(data["amount"])
        if isinstance(data.get("date"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", data["date"]):
            out["date"] = data["date"]
        if data.get("mode") in ("UPI", "NEFT", "Account Transfer"):
            out["mode"] = data["mode"]
        out["ref"] = str(data.get("ref") or "")[:64]
        out["receiver"] = str(data.get("receiver") or "")[:80]
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Gmail-debit → disbursement suggestions
# ---------------------------------------------------------------------------

def detect_disbursement_suggestions(fe: "FinanceEngine", account_id: str,
                                    days: int = 90) -> list[dict]:
    """Unclaimed debits in the custodial account (synced from Gmail/CSV, no
    beneficiary yet) fuzzy-matched against beneficiaries. The user confirms;
    confirming links the EXISTING transaction — never creates a duplicate."""
    beneficiaries = fe.list_beneficiaries(account_id)
    if not beneficiaries:
        return []
    since = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    rows = fe.conn.execute(
        "SELECT id, date, amount, merchant, notes, source FROM transactions"
        " WHERE account_id=? AND amount<0 AND beneficiary_id IS NULL"
        " AND source NOT IN ('custodial_manual','sheet_import') AND date>=?"
        " ORDER BY date DESC LIMIT 100", (account_id, since)).fetchall()
    suggestions = []
    for r in rows:
        ben, score = match_beneficiary(
            beneficiaries, f"{r['merchant'] or ''} {r['notes'] or ''}")
        if ben is None:
            continue
        suggestions.append({
            "transaction_id": r["id"], "date": r["date"],
            "amount": abs(r["amount"]), "merchant": r["merchant"],
            "source": r["source"],
            "beneficiary_id": ben["id"], "beneficiary_name": ben["name"],
            "score": score,
        })
    return suggestions


# ---------------------------------------------------------------------------
# Smart prefill + anomaly pre-check (pure stats, no LLM)
# ---------------------------------------------------------------------------

def beneficiary_history(fe: "FinanceEngine", beneficiary_id: str,
                        limit: int = 6, part: str | None = None) -> list[dict]:
    q = ("SELECT date, ABS(amount) amt FROM transactions"
         " WHERE beneficiary_id=? AND amount<0")
    params: list = [beneficiary_id]
    if part is not None:
        q += " AND COALESCE(part,'')=?"
        params.append(part)
    q += " ORDER BY date DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in fe.conn.execute(q, params).fetchall()]


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def suggest_amount(history: list[dict]) -> tuple[float | None, str]:
    """(suggested_amount, trend_note) from recent disbursements — median of
    the last 3 cycles, with a human-readable why."""
    amts = [h["amt"] for h in history[:3]]
    if not amts:
        return None, ""
    if len(set(amts)) == 1:
        n = len(amts)
        return amts[0], (f"same for last {n} cycles" if n > 1 else "last cycle's amount")
    med = _median(amts)
    prev = history[1]["amt"] if len(history) > 1 else None
    last = history[0]["amt"]
    note = f"median of last {len(amts)} ({', '.join(f'{a:g}' for a in reversed(amts))})"
    if prev and prev > 0 and abs(last - prev) / prev >= 0.15:
        pct = round((last - prev) / prev * 100)
        note += f" — last cycle was {'+' if pct > 0 else ''}{pct}% vs the one before"
    return med, note


def anomaly_precheck(fe: "FinanceEngine", account_id: str,
                     beneficiary_id: str, amount: float,
                     part: str | None = None) -> list[dict]:
    """Soft warnings shown before confirm — never blocks. For split
    beneficiaries, pass the part so history/duplicate checks stay per-part."""
    warnings: list[dict] = []
    hist = beneficiary_history(fe, beneficiary_id, part=part)

    # 1. duplicate within this cycle (same beneficiary+part, last 20 days)
    recent_cut = (_dt.date.today() - _dt.timedelta(days=20)).isoformat()
    dq = ("SELECT date, ABS(amount) amt FROM transactions"
          " WHERE beneficiary_id=? AND amount<0 AND date>=?")
    dparams: list = [beneficiary_id, recent_cut]
    if part is not None:
        dq += " AND COALESCE(part,'')=?"
        dparams.append(part)
    dup = fe.conn.execute(dq + " ORDER BY date DESC LIMIT 1", dparams).fetchone()
    if dup:
        warnings.append({
            "check": "duplicate_recent",
            "message": f"Already sent {dup['amt']:g} on {dup['date']} — sending again this cycle?",
        })

    # 2. amount far off the usual
    amts = [h["amt"] for h in hist]
    if len(amts) >= 2 and amount > 0:
        med = _median(amts)
        if med > 0 and abs(amount - med) / med >= 0.5:
            warnings.append({
                "check": "amount_outlier",
                "message": f"{amount:g} is well off the usual ~{med:g} for this beneficiary.",
            })

    # 3. would leave too little for the others still unpaid this cycle
    balance = fe.custodial_balance(account_id)
    recent_paid = {r["beneficiary_id"] for r in fe.conn.execute(
        "SELECT DISTINCT beneficiary_id FROM transactions"
        " WHERE account_id=? AND amount<0 AND date>=? AND beneficiary_id IS NOT NULL",
        (account_id, recent_cut)).fetchall()}
    still_due = 0.0
    for b in fe.list_beneficiaries(account_id):
        if b["id"] in recent_paid or b["id"] == beneficiary_id:
            continue
        if b.get("tracking_only"):
            continue   # their money doesn't come from this account's pool
        if b.get("split_kind") == "parts" and (b.get("default_parts") or []):
            for p in b["default_parts"]:
                if not isinstance(p, dict) or not p.get("name"):
                    continue
                amt = p.get("amount")
                if not amt:
                    ph = beneficiary_history(fe, b["id"], limit=3, part=p["name"])
                    amt = _median([h["amt"] for h in ph]) if ph else 0
                still_due += amt or 0
            continue
        if b.get("expected_amount"):
            still_due += b["expected_amount"]
            continue
        bh = beneficiary_history(fe, b["id"], limit=3)
        if bh:
            still_due += _median([h["amt"] for h in bh])
    if still_due > 0 and balance - amount < still_due:
        warnings.append({
            "check": "balance_shortfall",
            "message": (f"After this, balance {balance - amount:g} won't cover the usual "
                        f"~{still_due:g} still due to the others this cycle."),
        })
    return warnings


# ---------------------------------------------------------------------------
# Cycle-close narrative
# ---------------------------------------------------------------------------

def cycle_close_status(fe: "FinanceEngine", account_id: str,
                       window_days: int = 7) -> dict:
    """A cycle is 'complete' when every active beneficiary has a disbursement
    within the last window_days. Returns the rows for the narrative."""
    since = (_dt.date.today() - _dt.timedelta(days=window_days)).isoformat()
    beneficiaries = fe.list_beneficiaries(account_id)
    if not beneficiaries:
        return {"complete": False, "rows": []}
    rows = []
    for b in beneficiaries:
        r = fe.conn.execute(
            "SELECT date, ABS(amount) amt FROM transactions"
            " WHERE beneficiary_id=? AND amount<0 AND date>=?"
            " ORDER BY date DESC LIMIT 1", (b["id"], since)).fetchone()
        rows.append({"name": b["name"], "date": r["date"] if r else None,
                     "amount": r["amt"] if r else None})
    return {"complete": all(r["amount"] is not None for r in rows), "rows": rows}


def cycle_narrative(fe: "FinanceEngine", account: dict, status: dict,
                    llm=None) -> tuple[str, str]:
    """(title, markdown_body). Deterministic body; optional one local-LLM
    insight line appended — a template fallback never breaks the note."""
    today = _dt.date.today().isoformat()
    total = sum(r["amount"] for r in status["rows"] if r["amount"])
    balance = fe.custodial_balance(account["id"])
    title = f"Custodial cycle closed — {account.get('nickname', 'account')} ({today})"
    lines = [f"All {len(status['rows'])} beneficiaries paid. Total out: {total:g}. "
             f"Remaining balance: {balance:g}.", ""]
    for r in status["rows"]:
        lines.append(f"- {r['name']}: {r['amount']:g} on {r['date']}")
    body = "\n".join(lines)
    if llm is not None:
        try:
            insight, model = llm.generate(
                "One short sentence (max 25 words) noting anything worth "
                "remembering about this custodial disbursement cycle — a "
                "change vs usual, or 'consistent with previous cycles'. "
                "Plain text, no preamble.",
                body, sensitive=True)
            insight = (insight or "").strip().splitlines()[0][:200]
            if insight and model != "template":
                body += f"\n\n> {insight}"
        except Exception:
            pass
    return title, body


# ---------------------------------------------------------------------------
# Chat context block (the /api/ask custodial tool reads the ledger through this)
# ---------------------------------------------------------------------------

CHAT_KEYWORDS = ("custodial", "disburse", "beneficiar", "refill", "in-trust", "in trust")


def chat_keywords_for(fe: "FinanceEngine", account_ids: list[str]) -> set[str]:
    """Static keywords + every beneficiary-name token, so 'how much did
    Eswari get' routes here without hardcoding names."""
    kws = set(CHAT_KEYWORDS)
    for aid in account_ids:
        for b in fe.list_beneficiaries(aid):
            kws.update(t for t in _tokens(b["name"]) if len(t) > 2)
    return kws


def chat_context(fe: "FinanceEngine", account: dict) -> str:
    """Compact factual block the chat LLM answers from (never raw rows)."""
    from .custodial import next_cycle_prefill, run_validation
    aid = account["id"]
    year = _dt.date.today().year
    lines = [f"Custodial account: {account.get('nickname')} "
             f"(balance {fe.custodial_balance(aid):g})"]
    prefill = next_cycle_prefill(fe, aid)
    if prefill.get("due_date"):
        lines.append(f"Next cycle due: {prefill['due_date']} "
                     f"(cadence ~{prefill['cadence_days']} days)")
    for b in fe.list_beneficiaries(aid):
        tot_year = fe.conn.execute(
            "SELECT COALESCE(SUM(ABS(amount)),0) s, COUNT(*) n FROM transactions"
            " WHERE beneficiary_id=? AND amount<0 AND date>=?",
            (b["id"], f"{year}-01-01")).fetchone()
        hist = beneficiary_history(fe, b["id"], limit=1)
        last = f"last {hist[0]['amt']:g} on {hist[0]['date']}" if hist else "never logged"
        tag = " [tracking-only, not from this balance]" if b.get("tracking_only") else ""
        lines.append(f"- {b['name']}: {tot_year['s']:g} across {tot_year['n']} "
                     f"transfers in {year}; {last}{tag}")
    issues = run_validation(fe, aid).get("issues", [])
    if issues:
        lines.append("Open validation issues: " + "; ".join(
            f"{i['check']}({i.get('beneficiary', i.get('due_date', ''))})" for i in issues))
    return "\n".join(lines)
