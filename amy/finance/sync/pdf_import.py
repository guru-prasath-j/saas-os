"""PDF bank statement importer.

Fast path  — pdfplumber table extraction (no API cost, instant).
Fallback   — NVIDIA Nemotron / LLM for non-standard / scanned PDFs.

Flow:
  1. Upload PDF bytes for a bank account.
  2. _parse_pdf_pdfplumber() tries to extract a table of transactions directly.
     Handles: bordered tables, text-aligned tables, HDFC CC split rows, and a
     regex fallback for plain-text CC statement layouts.
  3. If pdfplumber yields 0 rows, fall back to LLM (PyMuPDF text + LLM JSON).
  4. Transactions are inserted with the same dedup logic as the CSV importer.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import SyncProvider, SyncResult

if TYPE_CHECKING:
    from ..engine import FinanceEngine


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class PasswordRequired(Exception):
    """Raised when a PDF is password-protected and no (valid) password is given."""


# ---------------------------------------------------------------------------
# Shared helpers (used by both pdfplumber and LLM paths)
# ---------------------------------------------------------------------------

def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("₹", "").replace("Rs.", "").strip()) or None
    except (ValueError, TypeError):
        return None


def _parse_iso_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = str(raw).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    import datetime as _dt
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d/%m/%y", "%d-%m-%y",
                "%d %b, %Y", "%d-%b-%Y", "%d-%b-%y", "%d.%m.%Y", "%d.%m.%y",
                "%b %d, %Y"):
        try:
            return _dt.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# pdfplumber fast path
# ---------------------------------------------------------------------------

_PLB_DATE_WORDS  = {"date", "dt", "txn date", "txn dt", "trans date", "value date",
                     "posting date", "transaction date", "tran date", "tran. date",
                     "entry date", "effective date"}
_PLB_DESC_WORDS  = {"narration", "description", "particulars", "details",
                     "transaction details", "trans description", "remarks",
                     "transaction narration", "description/narration"}
_PLB_DEBIT_WORDS = {"debit", "withdrawal", "dr", "withdrawals", "debit amount",
                     "withdrawal amount", "paid out", "money out", "debits",
                     "debit(inr)", "withdrawal(inr)", "withdrawals(dr.)"}
_PLB_CREDIT_WORDS= {"credit", "deposit", "cr", "deposits", "credit amount",
                     "deposit amount", "paid in", "money in", "credits",
                     "credit(inr)", "deposit(inr)"}
# Some CC statements (e.g. HDFC) use one combined amount column with a
# trailing "Cr" marker on credit rows instead of separate debit/credit columns.
_PLB_AMOUNT_WORDS = {"amount", "amount (in rs.)", "amount(in rs.)", "amount in rs.",
                      "amount in rs", "amt", "transaction amount"}
_CR_DR_SUFFIX_RE = re.compile(r"(?i)(?:cr|dr)\.?\s*$")
_CR_SUFFIX_RE = re.compile(r"(?i)cr\.?\s*$")


def _split_combined_amount(raw: str) -> float | None:
    """Strip a trailing Cr/Dr marker so _to_float can parse the number."""
    return _to_float(_CR_DR_SUFFIX_RE.sub("", str(raw or "")).strip())

_FOOTER_RE = re.compile(
    r"opening balance|closing balance|end of statement|generated on|"
    r"statement summary|gstin|gstn|senapati bapat", re.IGNORECASE
)


def _merge_split_rows(txns: list[dict]) -> list[dict]:
    """Merge HDFC CC rows where the description and amount are on separate lines."""
    if not txns:
        return txns
    merged: list[dict] = []
    i = 0
    while i < len(txns):
        row = dict(txns[i])
        if (row.get("debit") is None and row.get("credit") is None
                and i + 1 < len(txns)):
            nxt = txns[i + 1]
            if (not nxt.get("date")
                    and (nxt.get("debit") is not None or nxt.get("credit") is not None)):
                row["debit"] = nxt.get("debit")
                row["credit"] = nxt.get("credit")
                extra = nxt.get("description", "")
                if extra:
                    row["description"] = f"{row.get('description', '')} {extra}".strip()
                merged.append(row)
                i += 2
                continue
        merged.append(row)
        i += 1
    return merged


def _read_pdf_as_text(text: str) -> list[dict]:
    """
    Regex fallback for CC-style text where table extraction finds nothing.
    Matches lines like:  28/06/2026  AMAZON.IN  2,599.00  Cr
    """
    txns: list[dict] = []
    _LINE_RE = re.compile(
        r"(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})"   # date
        r"\s+"
        r"(.{4,60}?)"                               # description (non-greedy)
        r"\s+"
        r"([\d,]+\.\d{2})"                         # amount
        r"\s*(Cr|CR)?",                             # optional credit marker
        re.MULTILINE,
    )
    for m in _LINE_RE.finditer(text):
        date_str, desc, amt_str, cr_flag = m.groups()
        iso = _parse_iso_date(date_str.strip())
        if not iso:
            continue
        amount = _to_float(amt_str)
        if not amount:
            continue
        txns.append({
            "date": iso,
            "description": desc.strip(),
            "credit": amount if cr_flag else None,
            "debit": None if cr_flag else amount,
        })
    return txns


def _parse_pdf_pdfplumber(raw: bytes, password: str | None = None) -> list[dict]:
    """
    Primary PDF parser: pdfplumber table extraction — no LLM, no API cost.
    Returns [] if pdfplumber is not installed, the PDF is unreadable, or no
    transactions can be found (caller should fall back to LLM).
    """
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        return []

    import io

    try:
        open_kwargs: dict = {}
        if password:
            open_kwargs["password"] = password

        all_rows: list[list] = []
        fallback_text = ""

        with pdfplumber.open(io.BytesIO(raw), **open_kwargs) as pdf:
            for page in pdf.pages:
                # Strategy 1: strict line-based table extraction (bordered tables)
                tables = page.extract_tables({
                    "vertical_strategy": "lines",
                    "horizontal_strategy": "lines",
                }) or []
                # Strategy 2: text-position-based (borderless / HDFC-style tables)
                if not tables:
                    tables = page.extract_tables({
                        "vertical_strategy": "text",
                        "horizontal_strategy": "text",
                    }) or []
                for tbl in tables:
                    all_rows.extend(tbl)
                fallback_text += (page.extract_text() or "") + "\n"

        # ── Locate the header row ────────────────────────────────────────────
        # Some bank templates (e.g. HDFC "duplicate statement") render a whole
        # section as one bordered box with no internal grid lines, so
        # pdfplumber returns it as a single giant multi-line cell. Reject those
        # — a real header cell is a short single-line label — otherwise its
        # embedded "date"/"description" substrings get mistaken for a header.
        header_idx: int | None = None
        header_cells: list[str] = []
        for i, row in enumerate(all_rows):
            cells = [str(c or "").lower().strip() for c in row]
            if any(len(c) > 40 or "\n" in c for c in cells):
                continue
            has_date = any(c in _PLB_DATE_WORDS or "date" in c for c in cells)
            has_desc = any(c in _PLB_DESC_WORDS or "narr" in c or "desc" in c
                           or "particular" in c for c in cells)
            if has_date and has_desc:
                header_idx = i
                header_cells = cells
                break

        if header_idx is None:
            # No recognizable table header — try text-line regex for CC PDFs
            return _read_pdf_as_text(fallback_text)

        # ── Map columns ──────────────────────────────────────────────────────
        # Word-boundary matching, not raw substring — otherwise short tokens
        # like "cr"/"dr" spuriously match inside unrelated words (e.g. "cr"
        # inside "description"), misassigning the credit/debit column.
        def _col(*names: str) -> int | None:
            for name in names:
                pattern = re.compile(r"\b" + re.escape(name) + r"\b")
                for ci, h in enumerate(header_cells):
                    if h == name or pattern.search(h):
                        return ci
            return None

        date_i   = _col(*_PLB_DATE_WORDS)
        desc_i   = _col(*_PLB_DESC_WORDS)
        debit_i  = _col(*_PLB_DEBIT_WORDS)
        credit_i = _col(*_PLB_CREDIT_WORDS)
        # Combined amount column (e.g. HDFC CC: "Amount (in Rs.)" with a
        # trailing "Cr" marker on credit rows) — only relevant if there's no
        # separate debit/credit column.
        amount_i = (_col(*_PLB_AMOUNT_WORDS)
                    if debit_i is None and credit_i is None else None)

        if date_i is None or desc_i is None:
            return _read_pdf_as_text(fallback_text)

        def _cell(row: list, idx: int | None) -> str:
            if idx is None or idx >= len(row):
                return ""
            return str(row[idx] or "").strip()

        # ── Parse data rows ──────────────────────────────────────────────────
        txns: list[dict] = []
        for row in all_rows[header_idx + 1:]:
            if not row:
                continue
            row_str = " ".join(str(c or "") for c in row)
            if _FOOTER_RE.search(row_str):
                # Skip, don't stop — a footer/branding row can repeat at each
                # page boundary (e.g. HDFC's GSTIN strip), and real
                # transactions can continue on the next page. The date/
                # description checks below already reject genuine summary
                # rows, so this doesn't reopen the door to misparsing those.
                continue

            date_raw = _cell(row, date_i)
            desc_raw = _cell(row, desc_i)
            if not date_raw.strip("- \t*") or not desc_raw:
                continue

            iso = _parse_iso_date(date_raw)
            if not iso:
                continue

            if amount_i is not None:
                amt_raw = _cell(row, amount_i)
                amt = _split_combined_amount(amt_raw)
                is_credit = bool(_CR_SUFFIX_RE.search(amt_raw))
                debit  = None if is_credit else amt
                credit = amt if is_credit else None
            else:
                debit  = _to_float(_cell(row, debit_i))  if debit_i  is not None else None
                credit = _to_float(_cell(row, credit_i)) if credit_i is not None else None

            if (not debit or debit == 0) and (not credit or credit == 0):
                continue

            txns.append({
                "date": iso,
                "description": desc_raw,
                "debit":  debit  if debit  and debit  > 0 else None,
                "credit": credit if credit and credit > 0 else None,
            })

        txns = _merge_split_rows(txns)
        # Safety net: a header row can be legitimately found on a page whose
        # own table is nearly empty, while the real transactions live in an
        # unstructured block elsewhere (see the header-detection comment
        # above). If the table parse looks suspiciously thin, prefer whichever
        # extraction actually found more transactions.
        if len(txns) <= 1:
            text_txns = _read_pdf_as_text(fallback_text)
            if len(text_txns) > len(txns):
                return text_txns
        return txns

    except Exception:
        return []


# ---------------------------------------------------------------------------
# LLM fallback path (PyMuPDF text extraction + NVIDIA Nemotron / any LLM)
# ---------------------------------------------------------------------------

def _extract_text(raw: bytes, password: str | None = None) -> str:
    """Extract all text from a PDF via PyMuPDF."""
    import fitz  # PyMuPDF
    doc = fitz.open(stream=raw, filetype="pdf")
    if doc.needs_pass:
        if password is None:
            raise PasswordRequired(
                "This PDF is password-protected. "
                "Re-upload and supply the statement password."
            )
        ok = doc.authenticate(password)
        if not ok:
            raise PasswordRequired("Incorrect PDF password. Please check and retry.")
    return "\n".join(page.get_text() for page in doc)


_SYSTEM_PROMPT = """You are a precise bank-statement parser for Indian bank accounts.
Given raw text from a bank statement PDF, extract ALL transactions.
Return ONLY a valid JSON array — no explanation, no markdown fences.
Each element must have exactly these keys:
  "date"        : ISO date string "YYYY-MM-DD"
  "description" : narration / description string
  "debit"       : withdrawal amount as float, or null
  "credit"      : deposit amount as float, or null
  "balance"     : running balance as float, or null

Amount values must be plain numbers (no commas, no currency symbols).
If a value is absent or unclear, use null.
If no transactions are found, return [].
"""

_USER_PROMPT = """Extract all transactions from the following bank statement text.
Return a JSON array only.

--- STATEMENT TEXT ---
{text}
--- END ---
"""


def _parse_llm_json(raw: str) -> list[dict]:
    raw = re.sub(r"```(?:json)?", "", raw).strip()
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(raw[start:end + 1])
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def extract_transactions_llm(text: str, llm) -> list[dict]:
    """Call the LLM and return a list of raw transaction dicts."""
    try:
        prompt = _USER_PROMPT.format(text=text[:12000])
        raw, _ = llm.generate(_SYSTEM_PROMPT, prompt, sensitive=False)
        return _parse_llm_json(raw)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Import pipeline (shared dedup logic with csv_import)
# ---------------------------------------------------------------------------

_GENERIC_WORDS = frozenset({
    'neft','transaction','transactions','received','fund','transfer','credited',
    'debited','amount','remitted','your','account','bank','upi','payment',
    'to','from','by','at','in','the','a','an','is','are','via','of','for',
    'sent','into','beneficiary','name','inr','rs','towards','successfully',
    # Structural/boilerplate tokens shared across many unrelated statement
    # lines (e.g. HDFC's own internal voucher-ID prefix, or "RATE"/"REF" on
    # every tax line) — without excluding these, distinct entries like an
    # SGST and CGST split of the same reference number get treated as near-
    # duplicates of each other and one silently gets dropped on import.
    'vps','rate','ref',
})


def _desc_tokens(desc: str):
    words = {w.lower() for w in re.findall(r'[A-Z]{3,}', desc.upper())} - _GENERIC_WORDS
    nums  = set(re.findall(r'\d{8,}', desc))
    return words, nums


def _norm_desc(desc: str) -> str:
    """Alphanumeric-only, uppercased — collapses spacing/wording variants like
    'Life Style International' vs 'Lifestyle', which commonly happens when the
    same real purchase is described differently across sources (e.g. two
    different bank alert emails, or a PDF vs an email, for one transaction)."""
    return re.sub(r'[^A-Z0-9]', '', desc.upper())


def _is_near_duplicate(engine, date, amount, account_id, new_desc):
    """True if an existing row for same date+amount+account is the same transaction."""
    rows = engine.conn.execute(
        "SELECT merchant FROM transactions"
        " WHERE date=? AND amount=? AND account_id=?",
        (date, amount, account_id),
    ).fetchall()
    if not rows:
        return False
    new_words, new_nums = _desc_tokens(new_desc)
    new_norm = _norm_desc(new_desc)
    for (existing_desc,) in rows:
        ex_words, ex_nums = _desc_tokens(existing_desc)
        if new_nums and (new_nums - ex_nums):
            continue
        if not new_words and not new_nums:
            return True
        if new_words & ex_words:
            return True
        ex_norm = _norm_desc(existing_desc)
        if len(new_norm) >= 5 and len(ex_norm) >= 5 and (
            new_norm in ex_norm or ex_norm in new_norm
        ):
            return True
    return False


def parse_pdf_preview_only(
    raw: bytes,
    password: str | None = None,
    llm=None,
    account_type: str = "",
) -> list[dict]:
    """Parse PDF and return transaction dicts WITHOUT writing to the DB."""
    from ..categorizer import categorize as _cat

    raw_txns = _parse_pdf_pdfplumber(raw, password=password)
    if not raw_txns and llm is not None:
        try:
            text = _extract_text(raw, password=password)
            raw_txns = extract_transactions_llm(text, llm)
        except Exception:
            pass

    txns: list[dict] = []
    for row in raw_txns:
        iso_date = _parse_iso_date(row.get("date"))
        if not iso_date:
            continue
        description = str(row.get("description") or "").strip()
        debit  = _to_float(row.get("debit"))
        credit = _to_float(row.get("credit"))
        if debit is not None and (credit is None or credit == 0):
            amount = -(abs(debit))
        elif credit is not None and (debit is None or debit == 0):
            amount = abs(credit)
        elif debit is not None and credit is not None:
            amount = abs(credit) - abs(debit)
        else:
            continue
        txns.append({
            "date": iso_date,
            "description": description,
            "amount": round(amount, 2),
            "category": _cat(description, amount, account_type=account_type),
        })
    return txns


def parse_and_import_pdf(
    raw_transactions: list[dict],
    engine: "FinanceEngine",
    account_id: str,
    category: str = "Uncategorized",
) -> SyncResult:
    result = SyncResult()
    acc = engine.get_account(account_id)
    account_type = (acc or {}).get("account_type", "")

    for row in raw_transactions:
        iso_date = _parse_iso_date(row.get("date"))
        if not iso_date:
            result.errors.append(f"Skipping row — bad date: {row.get('date')!r}")
            continue

        description = str(row.get("description") or "").strip()
        debit  = _to_float(row.get("debit"))
        credit = _to_float(row.get("credit"))

        if debit is not None and (credit is None or credit == 0):
            amount = -(abs(debit))
        elif credit is not None and (debit is None or debit == 0):
            amount = abs(credit)
        elif debit is not None and credit is not None:
            amount = abs(credit) - abs(debit)
        else:
            result.errors.append(f"Skipping row — no debit/credit: {row!r}")
            continue

        # Exact dedup
        existing = engine.conn.execute(
            "SELECT id FROM transactions"
            " WHERE date=? AND amount=? AND merchant=? AND account_id=? LIMIT 1",
            (iso_date, amount, description, account_id),
        ).fetchone()
        if existing:
            result.skipped += 1
            continue

        # Near-dedup
        if _is_near_duplicate(engine, iso_date, amount, account_id, description):
            result.skipped += 1
            continue

        src = row.get("source", "pdf") if isinstance(row, dict) else "pdf"
        from ..categorizer import categorize as _cat
        # row.get("category") is set by the NVIDIA enrichment step in gmail_import
        row_cat = row.get("category", "") if isinstance(row, dict) else ""
        if row_cat and row_cat != "Uncategorized":
            effective_cat = row_cat
        elif category != "Uncategorized":
            effective_cat = category
        else:
            effective_cat = _cat(description, amount, account_type=account_type)
        tid = engine.add_transaction(
            amount=amount,
            category=effective_cat,
            merchant=description,
            date=iso_date,
            source=src,
            notes="",
            account_id=account_id,
        )
        result.transactions.append(
            {"id": tid, "date": iso_date, "amount": amount,
             "merchant": description, "account_id": account_id}
        )
        result.imported += 1

    return result


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class PDFImportProvider(SyncProvider):
    method = "pdf"

    def available(self) -> bool:
        try:
            import pdfplumber  # noqa: F401
            return True
        except ImportError:
            pass
        try:
            import fitz  # noqa: F401
            return True
        except ImportError:
            return False

    def import_from_bytes(
        self,
        raw: bytes,
        engine: "FinanceEngine",
        account_id: str,
        llm=None,
        password: str | None = None,
        category: str = "Uncategorized",
    ) -> SyncResult:
        if engine.get_account(account_id) is None:
            raise ValueError(f"Account {account_id!r} not found")

        # ── Fast path: pdfplumber (no API cost) ─────────────────────────────
        raw_txns = _parse_pdf_pdfplumber(raw, password=password)

        # ── Fallback: LLM for scanned / non-standard PDFs ───────────────────
        if not raw_txns:
            if llm is None:
                result = SyncResult()
                result.errors.append(
                    "pdfplumber could not extract transactions. "
                    "No LLM configured — try exporting as CSV/XLS instead."
                )
                return result
            try:
                text = _extract_text(raw, password=password)
            except PasswordRequired:
                raise
            except Exception as exc:
                result = SyncResult()
                result.errors.append(f"PDF text extraction failed: {exc}")
                return result
            raw_txns = extract_transactions_llm(text, llm)

        result = parse_and_import_pdf(raw_txns, engine, account_id, category)
        engine.touch_account(account_id)
        return result
