"""Gmail bank-statement email parser.

OAuth scope situation:
  The existing google.py connector already requests 'gmail.readonly' in SCOPES.
  This scope grants full message-read access (headers + body + attachments).
  No re-consent is needed for users who already authorized the Google connector —
  the scope is already sufficient.
  Users who have NOT yet linked Google will go through the existing OAuth flow
  (which already includes gmail.readonly) and will land with full access.

What this module does:
  1. Search Gmail for bank-alert / e-statement emails within a date window.
  2. Fetch full message body (plain-text preferred, HTML stripped to text).
  3. Use an LLM to extract transaction rows from each email.
  4. Insert via the same dedup pipeline as csv_import / pdf_import.
"""
from __future__ import annotations

import base64
import json
import re
from typing import TYPE_CHECKING

from . import SyncProvider, SyncResult
from .pdf_import import extract_transactions_llm, parse_and_import_pdf

if TYPE_CHECKING:
    from ..engine import FinanceEngine

# ---------------------------------------------------------------------------
# Default search terms for Indian bank statement/alert emails
# ---------------------------------------------------------------------------

# Compiled once — checked at message level before ANY parsing (regex or LLM).
# Matches failed/declined payment notifications so they are never imported.
_DECLINED_RE = re.compile(
    r"\b(declined|unsuccessful|could not be processed|not successful"
    r"|payment.{0,20}failed|autopay.{0,20}declined"
    r"|transaction.{0,20}failed|transaction.{0,20}unsuccessful"
    r"|insufficient funds|payment not processed|not authorized)\b",
    re.IGNORECASE,
)

DEFAULT_QUERY = (
    # Target the known HDFC InstaAlert sender directly — most precise
    "from:(alerts@hdfcbank.bank.in OR alerts@hdfcbank.net"
    " OR alerts@icicibank.com OR alerts@axisbank.com"
    " OR alerts@sbi.co.in OR alerts@kotakbank.com"
    " OR noreply@hdfcbank.com OR alerts@yesbank.in"
    " OR alerts@indusind.com OR alerts@federalbank.co.in"
    " OR alerts@idfcfirstbank.com OR alerts@pnb.co.in)"
    # Fallback: subject-based for other banks
    " OR subject:(\"has been debited\" OR \"has been credited\""
    " OR \"payment was made using your Credit Card\""
    " OR \"Account update for your HDFC Bank\""
    " OR \"transaction alert\" OR \"debit alert\" OR \"credit alert\")"
)

# ---------------------------------------------------------------------------
# Bank detection from email sender / body
# ---------------------------------------------------------------------------

_SENDER_BANK_MAP = [
    (re.compile(r"hdfcbank\.", re.I),         "HDFC Bank"),
    (re.compile(r"icicibank\.", re.I),        "ICICI Bank"),
    (re.compile(r"axisbank\.", re.I),         "Axis Bank"),
    (re.compile(r"sbi\.co\.in", re.I),        "SBI"),
    (re.compile(r"kotakbank\.", re.I),        "Kotak Bank"),
    (re.compile(r"yesbank\.", re.I),          "Yes Bank"),
    (re.compile(r"indusind\.", re.I),         "IndusInd Bank"),
    (re.compile(r"federalbank\.", re.I),      "Federal Bank"),
    (re.compile(r"idfcfirstbank\.", re.I),    "IDFC First Bank"),
    (re.compile(r"pnb\.co\.in", re.I),        "PNB"),
    (re.compile(r"unionbankofindia\.", re.I), "Union Bank"),
    (re.compile(r"canarabank\.", re.I),       "Canara Bank"),
    (re.compile(r"bankofbaroda\.", re.I),     "Bank of Baroda"),
    (re.compile(r"rblbank\.", re.I),          "RBL Bank"),
    (re.compile(r"bandhanbank\.", re.I),      "Bandhan Bank"),
]

_BODY_BANK_MAP = [
    (re.compile(r"\bHDFC Bank\b", re.I),     "HDFC Bank"),
    (re.compile(r"\bICICI Bank\b", re.I),    "ICICI Bank"),
    (re.compile(r"\bAxis Bank\b", re.I),     "Axis Bank"),
    (re.compile(r"\bState Bank\b", re.I),    "SBI"),
    (re.compile(r"\bKotak\b", re.I),         "Kotak Bank"),
    (re.compile(r"\bYes Bank\b", re.I),      "Yes Bank"),
    (re.compile(r"\bIndusInd\b", re.I),      "IndusInd Bank"),
    (re.compile(r"\bIDFC First\b", re.I),    "IDFC First Bank"),
    (re.compile(r"\bFederal Bank\b", re.I),  "Federal Bank"),
    (re.compile(r"\bPunjab National\b", re.I),"PNB"),
]


def _detect_bank(from_addr: str, body: str) -> str:
    """Return the bank name from the email sender domain (primary) or body keywords."""
    for pattern, name in _SENDER_BANK_MAP:
        if pattern.search(from_addr):
            return name
    for pattern, name in _BODY_BANK_MAP:
        if pattern.search(body):
            return name
    return "Bank"


# ---------------------------------------------------------------------------
# Merchant description extraction (12 ordered patterns)
# ---------------------------------------------------------------------------

_DESC_JUNK = frozenset({
    "your", "the", "account", "bank", "hdfc", "icici", "sbi", "axis", "kotak",
    "yes", "credit", "debit", "card", "transaction", "payment", "transfer",
    "amount", "rupees", "inr", "rs", "upi", "neft", "imps", "rtgs",
    "beneficiary", "name", "sender", "customer", "alert", "message",
})

_DESC_PATTERNS = [
    # CC: "towards MERCHANT on date" (HDFC CC alerts)
    re.compile(r"\btowards\s+([A-Za-z][A-Za-z0-9\s&\-\'\.]{2,45}?)\s+on\s+\d", re.IGNORECASE),
    # CC: "at MERCHANT on date" (HDFC CC alerts)
    re.compile(r"\bat\s+([A-Za-z][A-Za-z0-9\s&\-\'\.]{2,45}?)\s+on\s+\d", re.IGNORECASE),
    # UPI 4-part path: UPI/REF/MERCHANT/VPA
    re.compile(r"\bUPI\/[A-Z0-9]*\/([^\/]{3,40})\/", re.IGNORECASE),
    # UPI 2-part: "UPI-NAME-VPAREF@bank"
    re.compile(r"\bUPI[-\/]([A-Za-z][A-Za-z\s\-\']{2,35})-[A-Z0-9@]+", re.IGNORECASE),
    # VPA handle: "VPA merchant@bank"
    re.compile(r"\bVPA\s+([a-zA-Z][a-zA-Z0-9\._]{2,35})@", re.IGNORECASE),
    # SBI Info field: "Info: DESCRIPTION" or "Info: UPI/MERCHANT"
    re.compile(r"\bInfo[:\s]+(?:UPI\/)?([A-Za-z][A-Za-z0-9\s&\-\'\.]{2,40})", re.IGNORECASE),
    # AutoPay / subscription: "payment for YouTube Premium"
    re.compile(r"(?:autopay\s+)?payment\s+for\s+([A-Za-z][A-Za-z0-9\s&\-\'\.]{2,40})", re.IGNORECASE),
    # Generic "for MERCHANT" with word-boundary terminators
    re.compile(r"\bfor\s+([A-Za-z][A-Za-z0-9\s&\-\'\.]{3,40}?)(?:\s+(?:on|at|via|using)\b|\s*$)", re.IGNORECASE),
    # "sent to / transferred to NAME"
    re.compile(r"(?:transfer(?:red)?\s+to|sent\s+to)\s+([A-Za-z][A-Za-z\s\.]{2,35})", re.IGNORECASE),
    # "NEFT from / to / by NAME" — stop before amount or currency symbol
    re.compile(r"\bNEFT\s+(?:from|to|by)\s+([A-Za-z][A-Za-z\s]{2,35}?)(?=\s+(?:Rs|INR|₹|\d)|\s*$)", re.IGNORECASE),
    # "Merchant: NAME" explicit label
    re.compile(r"\bMerchant[:\s]+([A-Za-z][A-Za-z0-9\s&\-\'\.]{2,40})", re.IGNORECASE),
    # ALL-CAPS recipient at end of line (UPI narration style)
    re.compile(r"\bto\s+([A-Z][A-Z0-9\s]{4,35}?)(?:\s*$|\s+on\b)", re.MULTILINE),
]


def _extract_desc(text: str) -> str | None:
    """Return the cleanest merchant name found in `text`, or None if nothing matches."""
    for pat in _DESC_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        raw = m.group(1).strip()
        # Chop at verb phrases like "has been processed", "is scheduled", etc.
        raw = re.split(
            r"\s+(?:has|is|was|will|have|had|set|done|made|processed|"
            r"scheduled|initiated|completed|declined|failed)\b",
            raw, flags=re.IGNORECASE, maxsplit=1
        )[0].strip()
        # Strip trailing prepositions
        raw = re.sub(r"\s+(on|at|by|via|using|from|to|for)\s*$", "", raw, flags=re.IGNORECASE).strip()
        if not raw or len(raw) < 3:
            continue
        if raw.lower() in _DESC_JUNK:
            continue
        # ALL-CAPS → Title Case
        if raw.isupper():
            raw = raw.title()
        return raw
    return None


def _unwrap_body(body: str) -> str:
    """Strip outer forwarded-email wrapper so the inner original alert is parsed."""
    # Look for "---------- Forwarded message ----------" or "Begin forwarded message:"
    m = re.search(r"(?:Forwarded message|Begin forwarded message)[^\n]*\n", body, re.IGNORECASE)
    if m:
        return body[m.end():]
    return body

# ---------------------------------------------------------------------------
# Regex-based transaction extractor (no LLM needed for standard bank alerts)
# ---------------------------------------------------------------------------

def _try_regex_parse(subject: str, body: str, date_header: str, from_addr: str = "") -> list[dict]:
    """Parse Indian bank alert emails without LLM.

    Handles:
    1. HDFC Credit Card debit alert ("towards MERCHANT on date")
    2. HDFC Savings account credit/debit (structured a./b./c. fields)
    3. Generic Indian bank alert (amount + direction keywords)

    Falls back to LLM if nothing is found. Returns [].
    """
    import datetime as _dt

    body  = _unwrap_body(body)
    text  = f"{subject}\n{body}"
    bank  = _detect_bank(from_addr, body)

    def _amt(s: str) -> float:
        return float(s.replace(",", ""))

    def _to_iso(raw: str) -> str | None:
        raw = re.sub(r"\s+at\s+\d{1,2}:\d{2}.*", "", raw).strip().rstrip(",")
        for fmt in (
            "%d %b %Y", "%d %b, %Y", "%d-%b-%Y", "%d-%b-%y",
            "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%Y-%m-%d",
        ):
            try:
                return _dt.datetime.strptime(raw, fmt).date().isoformat()
            except Exception:
                continue
        return None

    # ── Pattern 1: HDFC Credit Card alert ─────────────────────────────────
    m = re.search(
        r"Rs\.?\s*([\d,]+(?:\.\d{1,2})?)\s+has been\s+(debited|credited)"
        r".{0,120}?towards\s+(.+?)\s+on\s+(\d{1,2}\s+\w{3},?\s*\d{4})",
        text, re.IGNORECASE | re.DOTALL,
    )
    if m:
        amt_str, direction, merchant, date_str = m.groups()
        iso  = _to_iso(date_str) or _dt.date.today().isoformat()
        sign = -1 if "debit" in direction.lower() else 1
        # Try to extract a cleaner merchant name via _extract_desc
        cleaner = _extract_desc(text)
        desc = (cleaner or merchant).strip()
        is_cc = bool(re.search(r"credit card", text, re.IGNORECASE))
        return [{"date": iso, "description": desc, "amount": sign * _amt(amt_str),
                 "source": "cc_gmail" if is_cc else "gmail", "bank": bank}]

    # ── Pattern 2: HDFC Savings — structured a./b./c. field format ────────
    m_amt = re.search(
        r"Rs\.?\s*([\d,]+(?:\.\d{1,2})?)\s+has been(?:\s+successfully)?\s+(credited|debited)",
        text, re.IGNORECASE,
    )
    if m_amt:
        amt_str, direction = m_amt.groups()
        is_debit = "debit" in direction.lower()
        if not is_debit:
            credited_to_you = bool(re.search(r"credited to your\b", text, re.IGNORECASE))
            is_debit = not credited_to_you
        sign = -1 if is_debit else 1

        m_date = re.search(r"\ba\.\s*Date:\s*(\d{2}-\d{2}-\d{2,4})", text, re.IGNORECASE)
        if not m_date:
            m_date = re.search(r"\b(\d{2}-\d{2}-\d{2,4})\b", text)
        iso = _to_iso(m_date.group(1)) if m_date else _dt.date.today().isoformat()

        # Description: prefer cleaner extraction over raw subject
        m_sender = re.search(r"\bb\.\s*Sender:\s*(.+?)(?:\s*\(VPA:|\n|$)", text, re.IGNORECASE)
        m_bene   = re.search(
            r"(?:credited to|remitted to|transferred to)\s+"
            r"(?:beneficiary(?:\s+name)?[:\s]+)?([A-Z][A-Za-z\s]{2,40})",
            text, re.IGNORECASE,
        )
        if m_sender:
            desc = m_sender.group(1).strip()
        elif m_bene and is_debit:
            desc = f"NEFT to {m_bene.group(1).strip()}"
        else:
            desc = _extract_desc(text) or re.sub(r"\s+", " ", subject).strip()[:80] or f"{bank} transfer"

        m_upi = re.search(r"UPI Reference No\.:\s*(\w+)", text, re.IGNORECASE)
        if m_upi:
            desc += f" [{m_upi.group(1)}]"

        return [{"date": iso, "description": desc, "amount": sign * _amt(amt_str), "bank": bank}]

    # ── Pattern 3: Generic multi-bank alert ───────────────────────────────
    # Extended to 7 patterns for broader coverage (covers INR/₹/Rs. prefix + suffix forms)
    _TXN_RE = [
        re.compile(r"Rs\.?\s*([\d,]+(?:\.\d{1,2})?)\s+(?:is\s+)?(?:has been\s+)?(debited|credited)", re.I),
        re.compile(r"INR\s*([\d,]+(?:\.\d{1,2})?)\s+(?:is\s+)?(debited|credited)", re.I),
        re.compile(r"₹\s*([\d,]+(?:\.\d{1,2})?)\s+(?:is\s+)?(debited|credited)", re.I),
        re.compile(r"(debited|credited)\s+(?:with\s+)?(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)", re.I),
        re.compile(r"(?:paid|sent|transferred)\s+(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)", re.I),
        re.compile(r"(?:received|credited)\s+(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)", re.I),
        re.compile(r"Amount[:\s]+(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)", re.I),
    ]

    for pat in _TXN_RE:
        m_g = pat.search(text)
        if not m_g:
            continue
        groups = m_g.groups()
        # Normalise: find which group is the amount (numeric)
        amt_str = direction = None
        for g in groups:
            if g and re.fullmatch(r"[\d,]+(?:\.\d{1,2})?", g):
                amt_str = g
            elif g and re.match(r"(?:debit|credit|paid|sent|received|transferred)", g, re.I):
                direction = g
        if not amt_str:
            continue

        # Determine sign
        if direction and re.match(r"(?:debit|paid|sent|transferred)", direction, re.I):
            sign = -1
        elif direction and re.match(r"(?:credit|received)", direction, re.I):
            sign = 1
            # "credited to [name]" = outgoing
            if re.search(r"credited to (?!your\b)", text, re.IGNORECASE):
                sign = -1
        else:
            sign = -1  # unknown → assume debit

        m_date = re.search(
            r"\b(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{1,2}\s+\w{3}\s+\d{4})\b", text
        )
        iso  = (_to_iso(m_date.group(1)) if m_date else None) or _dt.date.today().isoformat()
        desc = _extract_desc(text) or re.sub(r"\s+", " ", subject).strip()[:120] or f"{bank} alert"
        return [{"date": iso, "description": desc,
                 "amount": sign * _amt(amt_str), "bank": bank}]

    return []


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def _decode_body(data: str) -> str:
    """Decode base64url-encoded Gmail message part."""
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _strip_html(html: str) -> str:
    """Very lightweight HTML stripper — removes tags and normalises whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_body(payload: dict) -> str:
    """Recursively extract plain-text (preferred) or HTML body from a Gmail payload."""
    mime = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime == "text/plain" and body_data:
        return _decode_body(body_data)

    if mime == "text/html" and body_data:
        return _strip_html(_decode_body(body_data))

    # multipart — recurse into parts
    plain, html = "", ""
    for part in payload.get("parts", []):
        text = _extract_body(part)
        if part.get("mimeType") == "text/plain" and text:
            plain = text
        elif part.get("mimeType") == "text/html" and text:
            html = text
        elif text:
            plain = plain or text

    return plain or html


def _build_date_query(since: str | None, until: str | None) -> str:
    """Convert ISO date strings to Gmail `after:` / `before:` tokens."""
    parts = []
    if since:
        parts.append("after:" + since.replace("-", "/"))
    if until:
        parts.append("before:" + until.replace("-", "/"))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# NVIDIA Nemotron enrichment — batch clean merchant names + categorize
# ---------------------------------------------------------------------------

_ENRICH_SYSTEM = """You are an Indian bank transaction enricher.
Given raw bank alert transactions, return a JSON array.

For each transaction provide:
  "idx"      : the index number exactly as given
  "merchant" : clean merchant / payee name (1-5 words, Title Case)
               — NO bank boilerplate (HDFC/ICICI/SBI/UPI/NEFT/IMPS etc.)
               — NO status words (declined, failed, successful, processed, debited, credited)
               — Examples: "Swiggy", "YouTube Premium", "Eswari M", "Amazon"
  "category" : one of: Food, Transport, Utilities, Entertainment, Health, Shopping,
               Investment, Rent, Education, Travel, Insurance, EMI/Loan, Transfer,
               Income, Uncategorized

Rules:
- Personal UPI / NEFT / IMPS transfers between people → Transfer
- Salary credit / NEFT inbound from employer → Income
- Swiggy / Zomato / restaurant → Food
- Amazon / Flipkart / Meesho → Shopping
- Netflix / Hotstar / YouTube Premium / Spotify → Entertainment
- Electricity / Jio / Airtel / broadband → Utilities
- Hospital / pharmacy → Health
- If truly unclear → Uncategorized

Return ONLY the JSON array, no explanation."""


def _enrich_with_llm(
    txns: list[dict],
    snippet_map: dict[int, str],
    llm,
) -> list[dict]:
    """
    Batch-enrich Gmail transactions using NVIDIA Nemotron.

    txns        — list of raw transaction dicts (mutated in-place, also returned)
    snippet_map — {txn_index: email_body_snippet} for context
    llm         — LLMRouter instance
    """
    if not txns or llm is None:
        return txns

    # Pre-categorize with fast keyword rules first
    from ..categorizer import categorize as _kw_cat
    for t in txns:
        if not t.get("_kw_cat"):
            t["_kw_cat"] = _kw_cat(t.get("description", ""), t.get("amount", 0.0))

    # Select candidates that need LLM enrichment
    candidates = []
    for i, t in enumerate(txns):
        desc = t.get("description", "")
        kw_cat = t.get("_kw_cat", "Uncategorized")
        # Enrich if: still uncategorized, OR description is long/raw bank narration
        if (kw_cat == "Uncategorized"
                or len(desc) > 55
                or re.search(r"\b(neft|upi.?ref|imps|rtgs|ref\s*no)\b", desc, re.I)):
            ctx = snippet_map.get(i, "")[:180]
            candidates.append({
                "idx": i,
                "raw": desc[:80],
                "ctx": ctx,
                "amt": t.get("amount", 0.0),
            })

    if not candidates:
        # Apply keyword categories and return
        for t in txns:
            cat = t.pop("_kw_cat", None)
            if cat and cat != "Uncategorized" and "category" not in t:
                t["category"] = cat
        return txns

    # Build concise batch prompt
    lines = "\n".join(
        f'{c["idx"]}. ₹{abs(c["amt"]):.0f} {"(income)" if c["amt"] > 0 else "(expense)"}'
        f' | Raw: {c["raw"]}'
        f'{" | Context: " + c["ctx"] if c["ctx"] else ""}'
        for c in candidates
    )

    try:
        raw_resp, _ = llm.generate(_ENRICH_SYSTEM, f"Enrich:\n{lines}", sensitive=False)
        raw_resp = re.sub(r"```(?:json)?", "", raw_resp).strip()
        start, end = raw_resp.find("["), raw_resp.rfind("]")
        if start != -1 and end != -1:
            for item in json.loads(raw_resp[start:end + 1]):
                idx = item.get("idx")
                if idx is None or not (0 <= idx < len(txns)):
                    continue
                if item.get("merchant"):
                    txns[idx]["description"] = item["merchant"]
                if item.get("category") and item["category"] != "Uncategorized":
                    txns[idx]["category"] = item["category"]
    except Exception:
        pass  # degraded gracefully — keyword categories still apply below

    # Apply keyword categories where LLM didn't assign one
    for t in txns:
        cat = t.pop("_kw_cat", None)
        if cat and cat != "Uncategorized" and "category" not in t:
            t["category"] = cat

    return txns


# ---------------------------------------------------------------------------
# Core sync
# ---------------------------------------------------------------------------

def sync_gmail(
    creds,
    engine: "FinanceEngine",
    account_id: str,
    llm,
    since: str | None = None,
    until: str | None = None,
    extra_query: str = "",
    max_messages: int = 200,
    category: str = "Uncategorized",
    cc_account_id: str | None = None,
    inbound_hook=None,
) -> SyncResult:
    """
    Fetch bank-related emails, extract transactions via LLM, and import them.

    creds  — google.oauth2.credentials.Credentials (from existing google.py logic)
    engine — FinanceEngine instance
    llm    — LLMRouter instance for LLM-based extraction
    inbound_hook — optional CareerInboundHook (amy/career_inbound.py, Part
    5D): adds ONE extra targeted messages.list for open applications' HR
    contacts inside this same sync pass (never a second poll loop) and
    routes those messages through career matching INSTEAD of the finance
    parser. None (the default) leaves this function's behavior untouched.
    """
    try:
        from googleapiclient.discovery import build
    except ImportError:
        result = SyncResult()
        result.errors.append(
            "google-api-python-client not installed. "
            "Run: pip install google-api-python-client")
        return result

    if engine.get_account(account_id) is None:
        result = SyncResult()
        result.errors.append(f"Account {account_id!r} not found")
        return result

    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)

    # Build search query
    date_q = _build_date_query(since, until)
    q_parts = [p for p in (DEFAULT_QUERY, date_q, extra_query) if p]
    query = " ".join(q_parts)

    res = svc.users().messages().list(
        userId="me", maxResults=max_messages, q=query).execute()
    messages = res.get("messages", [])

    combined_result = SyncResult()

    # ── Career inbound pass (Part 5D) — same sync, own precise query ────────
    # A separate targeted list call so HR replies never eat into the finance
    # message budget above; matched messages are handled by the hook and
    # excluded from finance parsing below.
    career_handled: set[str] = set()
    if inbound_hook is not None and getattr(inbound_hook, "active", False):
        career_q = (inbound_hook.gmail_query() or "").strip()
        if career_q:
            try:
                res2 = svc.users().messages().list(
                    userId="me",
                    maxResults=int(getattr(inbound_hook, "max_messages", 25)),
                    q=" ".join(p for p in (career_q, date_q) if p)).execute()
                for msg_ref in res2.get("messages", []) or []:
                    try:
                        msg = svc.users().messages().get(
                            userId="me", id=msg_ref["id"], format="full").execute()
                        payload = msg.get("payload", {})
                        hdrs = {h["name"].lower(): h["value"]
                                for h in payload.get("headers", [])}
                        if inbound_hook.handle(msg_ref["id"], hdrs,
                                               _extract_body(payload)):
                            career_handled.add(msg_ref["id"])
                    except Exception as exc:
                        combined_result.errors.append(
                            f"career inbound {msg_ref['id']}: {exc}")
            except Exception as exc:
                combined_result.errors.append(f"career inbound query: {exc}")

    # ── Pass 1: parse all emails, collect raw transactions ──────────────────
    # msg_batches: list of (raw_txns, email_body_snippet, is_cc_flag)
    msg_batches: list[tuple[list[dict], str, bool]] = []

    for msg_ref in messages:
        try:
            if msg_ref["id"] in career_handled:
                continue   # already consumed by the career inbound pass
            msg = svc.users().messages().get(
                userId="me", id=msg_ref["id"], format="full").execute()
            payload = msg.get("payload", {})
            hdr_dict = {h["name"]: h["value"] for h in payload.get("headers", [])}
            body = _extract_body(payload)

            if not body.strip():
                combined_result.skipped += 1
                continue

            subject  = hdr_dict.get("Subject", "")
            date_hdr = hdr_dict.get("Date", "")
            from_hdr = hdr_dict.get("From", "")
            context_text = f"Subject: {subject}\nDate: {date_hdr}\n\n{body}"

            # ── Message-level declined guard (before regex AND LLM) ───────────
            # Checks both subject and body so the LLM fallback can never import
            # a failed/declined payment notification.
            if _DECLINED_RE.search(f"{subject}\n{body}"):
                combined_result.skipped += 1
                continue

            # Regex parser (fast, no LLM, handles standard HDFC/ICICI/SBI alerts)
            raw_txns = _try_regex_parse(subject, body, date_hdr, from_addr=from_hdr)

            # LLM fallback for complex / multi-row e-statements
            if not raw_txns:
                try:
                    raw_txns = extract_transactions_llm(context_text, llm)
                except Exception as llm_exc:
                    combined_result.errors.append(
                        f"Message {msg_ref['id']}: LLM error — {llm_exc}")
                    continue
                # extract_transactions_llm is shared with the PDF importer and
                # doesn't know it's parsing an email — tag source here, else
                # it silently defaults to "pdf" in the shared insert pipeline,
                # and CC detection below (source == "cc_gmail") never fires.
                is_cc_msg = bool(re.search(r"credit card", context_text, re.IGNORECASE))
                for t in raw_txns:
                    t.setdefault("source", "cc_gmail" if is_cc_msg else "gmail")

            if not raw_txns:
                combined_result.skipped += 1
                continue

            is_cc = any(t.get("source") == "cc_gmail" for t in raw_txns)
            msg_batches.append((raw_txns, body[:250], is_cc))

        except Exception as exc:
            combined_result.errors.append(f"Message {msg_ref['id']}: {exc}")

    if not msg_batches:
        engine.touch_account(account_id)
        return combined_result

    # ── Pass 2: NVIDIA Nemotron batch enrichment (merchant + category) ───────
    flat_txns: list[dict] = []
    snippet_map: dict[int, str] = {}
    for raw_list, snippet, _ in msg_batches:
        for t in raw_list:
            snippet_map[len(flat_txns)] = snippet
            flat_txns.append(t)

    enriched = _enrich_with_llm(flat_txns, snippet_map, llm)

    # ── Pass 3: import enriched transactions with dedup ──────────────────────
    offset = 0
    for raw_list, _, is_cc in msg_batches:
        n = len(raw_list)
        batch = enriched[offset:offset + n]
        target_aid = cc_account_id if is_cc and cc_account_id else account_id
        r = parse_and_import_pdf(batch, engine, target_aid, category)
        combined_result.imported += r.imported
        combined_result.skipped  += r.skipped
        combined_result.errors.extend(r.errors)
        combined_result.transactions.extend(r.transactions)
        offset += n

    engine.touch_account(account_id)
    return combined_result


# ---------------------------------------------------------------------------
# Provider wrapper
# ---------------------------------------------------------------------------

class GmailImportProvider(SyncProvider):
    method = "gmail"

    def __init__(self, creds=None):
        self._creds = creds   # google.oauth2.credentials.Credentials or None

    def available(self) -> bool:
        return self._creds is not None

    def sync(
        self,
        engine: "FinanceEngine",
        account_id: str,
        llm,
        since: str | None = None,
        until: str | None = None,
        extra_query: str = "",
        max_messages: int = 200,
        category: str = "Uncategorized",
        cc_account_id: str | None = None,
    ) -> SyncResult:
        if self._creds is None:
            r = SyncResult()
            r.errors.append(
                "No Google credentials found. "
                "Link your Google account via the connectors section first.")
            return r
        return sync_gmail(
            self._creds, engine, account_id, llm,
            since=since, until=until,
            extra_query=extra_query,
            max_messages=max_messages,
            category=category,
            cc_account_id=cc_account_id,
        )
