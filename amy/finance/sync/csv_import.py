"""CSV bank statement importer.

Flow:
  1. Client uploads raw CSV bytes for a bank account.
  2. If a column mapping is already saved for this bank (bank_column_maps table),
     it is applied automatically.
  3. If no mapping exists and none is supplied, a preview response is returned
     with the detected headers + 3 sample rows — the client must POST again with
     a column_map to complete the import.
  4. On a successful import the mapping is saved for future uploads from the same bank.

Column map schema (JSON / dict):
  {
    "date":         <header name for transaction date>,
    "description":  <header name for narration / description>,
    "debit":        <header for debit / withdrawal amount>  OR null,
    "credit":       <header for credit / deposit amount>    OR null,
    "amount":       <header for a single signed amount>     OR null,
    "type":         <header for Dr/Cr flag>                 OR null,
    "date_format":  <strptime format, e.g. "%d/%m/%Y">      OR null (auto-detect)
  }
  Either (debit + credit) OR (amount) must be non-null.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
from typing import TYPE_CHECKING


def _html_table_to_csv(raw: bytes) -> bytes:
    """Parse an HTML file that contains a <table> and convert the largest table to CSV.
    Used for HDFC/IndianBank exports that are HTML saved with a .xls extension."""
    from html.parser import HTMLParser

    class _P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tables: list[list[list[str]]] = []
            self._t: list[list[str]] | None = None
            self._r: list[str] | None = None
            self._cell: list[str] | None = None

        def handle_starttag(self, tag, attrs):
            if tag == "table":
                self._t = []
            elif tag == "tr" and self._t is not None:
                self._r = []
            elif tag in ("td", "th") and self._r is not None:
                self._cell = []

        def handle_endtag(self, tag):
            if tag == "table" and self._t is not None:
                self.tables.append(self._t); self._t = None
            elif tag == "tr" and self._r is not None:
                if self._r: self._t.append(self._r)  # type: ignore[union-attr]
                self._r = None
            elif tag in ("td", "th") and self._cell is not None:
                self._r.append("".join(self._cell).strip())  # type: ignore[union-attr]
                self._cell = None

        def handle_data(self, data):
            if self._cell is not None:
                self._cell.append(data)

    text = _decode(raw)
    p = _P()
    p.feed(text)
    if not p.tables:
        raise ValueError("No HTML table found in file")
    # Pick the table with the most rows
    tbl = max(p.tables, key=len)
    out = io.StringIO()
    w = csv.writer(out)
    for row in tbl:
        w.writerow(row)
    return out.getvalue().encode()


def _xls_to_csv(raw: bytes, filename: str = "") -> bytes:
    """Convert .xls or .xlsx bytes to CSV bytes.
    Handles HDFC-style exports: HTML tables saved with a .xls extension."""
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    buf = io.BytesIO(raw)
    # Detect real OLE binary XLS by magic bytes (D0 CF 11 E0)
    is_ole = raw[:4] == b"\xd0\xcf\x11\xe0"
    # Detect ZIP (xlsx) by magic bytes
    is_zip = raw[:2] == b"PK"

    if ext == "xls" and not is_ole:
        # HDFC and some Indian banks export HTML as .xls — parse the table directly
        return _html_table_to_csv(raw)

    if is_zip or ext == "xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(buf, data_only=True)
        ws = wb.active
        out = io.StringIO()
        w = csv.writer(out)
        for row in ws.iter_rows(values_only=True):
            w.writerow(["" if v is None else str(v) for v in row])
        return out.getvalue().encode()

    # True binary XLS
    import xlrd
    wb = xlrd.open_workbook(file_contents=raw)
    ws = wb.sheet_by_index(0)
    out = io.StringIO()
    w = csv.writer(out)
    for row in range(ws.nrows):
        w.writerow([str(ws.cell_value(row, c)) for c in range(ws.ncols)])
    return out.getvalue().encode()

from . import SyncProvider, SyncResult

if TYPE_CHECKING:
    from ..engine import FinanceEngine

# ---------------------------------------------------------------------------
# Date parsing helpers
# ---------------------------------------------------------------------------

_DATE_FMTS = [
    "%d/%m/%Y", "%d/%m/%y",
    "%d-%m-%Y", "%d-%m-%y",
    "%Y-%m-%d",
    "%d %b %Y", "%d %b %y",
    "%b %d, %Y", "%d-%b-%Y", "%d-%b-%y",
    "%d.%m.%Y", "%d.%m.%y",
]


def _parse_date(raw: str, fmt: str | None = None) -> str | None:
    """Return ISO date string, or None if unparseable."""
    raw = raw.strip()
    if not raw:
        return None
    if fmt:
        try:
            return _dt.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    for f in _DATE_FMTS:
        try:
            return _dt.datetime.strptime(raw, f).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_amount(raw: str) -> float | None:
    """Strip currency symbols / commas and parse to float. Returns None on failure."""
    if raw is None:
        return None
    cleaned = raw.strip().replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs ", "")
    if not cleaned or cleaned == "-":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Encoding detection
# ---------------------------------------------------------------------------

def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("latin-1", errors="replace")


# ---------------------------------------------------------------------------
# Header-row detection  (handles bank statements with title rows at top)
# ---------------------------------------------------------------------------

# Expanded alias sets — used both for header-row detection and auto column mapping
_DATE_COLS = {
    "date", "dt", "txn date", "txn dt", "transaction date", "trans date",
    "posting date", "value date", "tran date", "tran. date", "entry date",
    "effective date", "transaction dt", "value dt",
}
_DESC_COLS = {
    "narration", "description", "particulars", "transaction details",
    "remarks", "details", "trans description", "description/narration",
    "transaction narration", "transaction description", "merchant",
    "beneficiary name", "payee", "chq./ref.no./tran id",
}
_DEBIT_COLS = {
    "debit", "withdrawal", "dr", "withdrawals", "debit amount",
    "withdrawal amount", "paid out", "money out", "debits",
    "debit(inr)", "withdrawal(inr)", "debit amt", "withdrawals(dr.)",
    "dr amount", "dr amt", "debit/dr",
}
_CREDIT_COLS = {
    "credit", "deposit", "cr", "deposits", "credit amount",
    "deposit amount", "paid in", "money in", "credits",
    "credit(inr)", "deposit(inr)", "credit amt", "deposit amt",
    "cr amount", "cr amt", "credit/cr",
}
_AMOUNT_COLS = {
    "amount", "amt", "transaction amount", "net amount", "txn amount",
}
_DR_CR_COLS = {
    "type", "txn type", "dr/cr", "cr/dr", "debit/credit",
    "transaction type", "dr cr", "indicator", "dr. / cr.", "credit/debit",
}

# Kept for header-row detection (kept by old name for _skip_to_header_row)
_HDR_DATE_WORDS = _DATE_COLS
_HDR_DESC_WORDS = _DESC_COLS


def _skip_to_header_row(text: str) -> str:
    """Return the text starting at the first row that looks like a real header row."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        cells = {c.strip().lower() for c in line.split(",") if c.strip()}
        has_date = bool(cells & _HDR_DATE_WORDS) or any("date" in c for c in cells)
        has_desc = bool(cells & _HDR_DESC_WORDS) or any(
            kw in c for c in cells for kw in ("narr", "desc", "particular", "remark")
        )
        if has_date and has_desc:
            return "".join(lines[i:])
    return text


def _find_col(headers: list[str], aliases: set[str]) -> str | None:
    """Return the first header (by file order) that exactly or partially matches any alias."""
    # Pass 1: exact match — respect header order in the file
    for h in headers:
        if h.strip().lower() in aliases:
            return h
    # Pass 2: partial match — any alias is a substring of the header
    for h in headers:
        h_lower = h.strip().lower()
        if any(alias in h_lower for alias in aliases):
            return h
    return None


def _auto_detect_columns(headers: list[str], sample_rows: list[dict]) -> dict | None:
    """Auto-detect column mapping from headers + sample data. Returns map or None."""
    date_col   = _find_col(headers, _DATE_COLS)
    desc_col   = _find_col(headers, _DESC_COLS)
    debit_col  = _find_col(headers, _DEBIT_COLS)
    credit_col = _find_col(headers, _CREDIT_COLS)
    amount_col = _find_col(headers, _AMOUNT_COLS)
    type_col   = _find_col(headers, _DR_CR_COLS)

    if not date_col or not desc_col:
        return None

    # If debit and credit resolved to the same column, it's actually a Dr/Cr indicator
    # column (like "Dr/Cr"), not separate amount columns. Fall back to amount_col.
    if debit_col and credit_col and debit_col == credit_col:
        if not type_col:
            type_col = debit_col
        debit_col = None
        credit_col = None

    if not debit_col and not credit_col and not amount_col:
        return None

    # Content-based Dr/Cr detection: scan unlabeled columns for DR/CR values
    if not type_col and amount_col:
        dr_cr_values = {"DR", "CR", "D", "C", "DEBIT", "CREDIT", ""}
        for h in headers:
            if h in (date_col, desc_col, amount_col):
                continue
            vals = {str(r.get(h, "")).strip().upper() for r in sample_rows}
            if vals and vals <= dr_cr_values:
                type_col = h
                break

    return {
        "date":        date_col,
        "description": desc_col,
        "debit":       debit_col,
        "credit":      credit_col,
        "amount":      amount_col if not debit_col and not credit_col else None,
        "type":        type_col,
    }


# ---------------------------------------------------------------------------
# Core parsing
# ---------------------------------------------------------------------------

def preview_csv(raw: bytes, max_rows: int = 3) -> dict:
    """Return headers + sample rows without importing anything."""
    text = _skip_to_header_row(_decode(raw))
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    rows = []
    for i, row in enumerate(reader):
        if i >= max_rows:
            break
        rows.append(dict(row))
    return {"headers": list(headers), "sample_rows": rows, "needs_mapping": True}


def parse_csv_preview_only(raw: bytes, column_map: dict, filename: str = "") -> list[dict]:
    """Parse CSV and return transaction dicts WITHOUT writing to the DB."""
    from ..categorizer import categorize as _cat

    # Only re-convert if it's still binary XLS/XLSX (magic bytes check).
    # The preview endpoint already converts before calling us, so skip if already CSV.
    if raw[:2] in (b"PK", b"\xd0\xcf"):
        raw = _xls_to_csv(raw, filename)

    text = _skip_to_header_row(_decode(raw))
    reader = csv.DictReader(io.StringIO(text))

    date_col   = column_map.get("date", "")
    desc_col   = column_map.get("description", "")
    debit_col  = column_map.get("debit")
    credit_col = column_map.get("credit")
    amount_col = column_map.get("amount")
    type_col   = column_map.get("type")
    date_fmt   = column_map.get("date_format")

    _FOOTER = {"statement summary", "opening balance", "closing balance summary",
               "generated on", "end of statement", "gstn", "gstin"}
    txns: list[dict] = []
    for row in reader:
        all_vals = " ".join(str(v) for v in row.values()).lower()
        if any(m in all_vals for m in _FOOTER):
            break
        raw_date = row.get(date_col, "").strip() if date_col else ""
        if not raw_date.strip("* \t-_="):
            continue
        iso_date = _parse_date(raw_date, date_fmt)
        if not iso_date:
            continue
        description = row.get(desc_col, "").strip() if desc_col else ""
        amount: float | None = None
        if debit_col or credit_col:
            debit  = _parse_amount(row.get(debit_col  or "", "")) if debit_col  else None
            credit = _parse_amount(row.get(credit_col or "", "")) if credit_col else None
            debit  = debit  or 0.0
            credit = credit or 0.0
            if debit == 0.0 and credit == 0.0:
                continue
            amount = credit - debit
        elif amount_col:
            parsed = _parse_amount(row.get(amount_col, ""))
            if parsed is None:
                continue
            if type_col:
                t = row.get(type_col, "").strip().upper()
                amount = -abs(parsed) if ("DR" in t or "DEBIT" in t or "WITHDRAWAL" in t) else abs(parsed)
            else:
                amount = parsed
        if amount is None:
            continue
        txns.append({
            "date": iso_date,
            "description": description,
            "amount": round(amount, 2),
            "category": _cat(description, amount),
        })
    return txns


def parse_and_import(
    raw: bytes,
    engine: "FinanceEngine",
    account_id: str,
    column_map: dict,
    category: str = "Uncategorized",
) -> SyncResult:
    """Parse CSV bytes using column_map and write transactions into finance.db."""
    text = _skip_to_header_row(_decode(raw))
    reader = csv.DictReader(io.StringIO(text))

    date_col = column_map.get("date", "")
    desc_col = column_map.get("description", "")
    debit_col = column_map.get("debit")
    credit_col = column_map.get("credit")
    amount_col = column_map.get("amount")
    type_col = column_map.get("type")
    date_fmt = column_map.get("date_format")

    # Footer/summary keywords — stop processing when any cell in a row matches
    _FOOTER_MARKERS = {
        "statement summary", "opening balance", "closing balance summary",
        "generated on", "end of statement", "gstn", "gstin",
        "registered office", "hdfcbank.com", "senapati bapat",
    }

    result = SyncResult()

    for row_num, row in enumerate(reader, start=2):
        # --- detect footer rows → stop entirely ---
        all_vals = " ".join(str(v) for v in row.values()).lower()
        if any(m in all_vals for m in _FOOTER_MARKERS):
            break   # everything below is bank boilerplate, not transactions

        # --- date ---
        raw_date = row.get(date_col, "").strip() if date_col else ""

        # Silently skip decorative / separator rows (empty, only *, only dashes)
        cleaned_date = raw_date.strip("* \t-_=")
        if not cleaned_date:
            result.skipped += 1
            continue

        iso_date = _parse_date(raw_date, date_fmt)
        if not iso_date:
            result.errors.append(f"Row {row_num}: unparseable date '{raw_date}'")
            continue

        # --- description ---
        description = row.get(desc_col, "").strip() if desc_col else ""

        # --- amount ---
        amount: float | None = None
        if debit_col or credit_col:
            debit = _parse_amount(row.get(debit_col or "", "")) if debit_col else None
            credit = _parse_amount(row.get(credit_col or "", "")) if credit_col else None
            debit = debit or 0.0
            credit = credit or 0.0
            if debit == 0.0 and credit == 0.0:
                result.skipped += 1
                continue
            amount = credit - debit   # positive = income, negative = expense
        elif amount_col:
            raw_amt = row.get(amount_col, "")
            parsed = _parse_amount(raw_amt)
            if parsed is None:
                result.errors.append(f"Row {row_num}: unparseable amount '{raw_amt}'")
                continue
            # Handle Dr/Cr type indicator
            if type_col:
                txn_type = row.get(type_col, "").strip().upper()
                if "DR" in txn_type or "DEBIT" in txn_type or "WITHDRAWAL" in txn_type:
                    amount = -abs(parsed)
                elif "CR" in txn_type or "CREDIT" in txn_type or "DEPOSIT" in txn_type:
                    amount = abs(parsed)
                else:
                    amount = parsed   # keep sign as-is
            else:
                amount = parsed
        else:
            result.errors.append(f"Row {row_num}: column_map has no amount columns")
            continue

        # --- deduplication: exact match ---
        existing = engine.conn.execute(
            "SELECT id FROM transactions"
            " WHERE date=? AND amount=? AND merchant=? AND account_id=? LIMIT 1",
            (iso_date, amount, description, account_id),
        ).fetchone()
        if existing:
            result.skipped += 1
            continue

        # --- near-dedup: same day+amount+account, generic/overlapping narration ---
        from .pdf_import import _is_near_duplicate
        if _is_near_duplicate(engine, iso_date, amount, account_id, description):
            result.skipped += 1
            continue

        from ..categorizer import categorize as _cat
        effective_cat = _cat(description, amount) if category == "Uncategorized" else category
        tid = engine.add_transaction(
            amount=amount,
            category=effective_cat,
            merchant=description,
            date=iso_date,
            source="csv",
            notes="",
            account_id=account_id,
        )
        txn = {"id": tid, "date": iso_date, "amount": amount,
               "merchant": description, "account_id": account_id}
        result.transactions.append(txn)
        result.imported += 1

    return result


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------

class CSVImportProvider(SyncProvider):
    method = "csv"

    def available(self) -> bool:
        return True   # no external dependencies

    def import_from_bytes(
        self,
        raw: bytes,
        engine: "FinanceEngine",
        account_id: str,
        column_map: dict | None = None,
        category: str = "Uncategorized",
        filename: str = "",
    ) -> "SyncResult | dict":
        """
        If column_map is None and no saved mapping for this account's bank:
            returns preview dict with needs_mapping=True.
        Otherwise:
            imports and returns SyncResult.
        Accepts .csv, .xls, and .xlsx — Excel files are auto-converted to CSV.
        """
        ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
        if ext in ("xls", "xlsx") or raw[:2] in (b"PK", b"\xd0\xcf"):  # magic bytes for xlsx/xls
            raw = _xls_to_csv(raw, filename)
        account = engine.get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id!r} not found")

        effective_map = column_map
        if effective_map is None:
            effective_map = engine.get_column_map(account["bank_name"])

        if effective_map is None:
            from .bank_presets import detect_preset
            preview = preview_csv(raw)

            # 1. Named bank preset (highest confidence)
            preset = detect_preset(preview["headers"])
            if preset is not None:
                engine.save_column_map(account["bank_name"], preset.column_map)
                result = parse_and_import(raw, engine, account_id,
                                          preset.column_map, category)
                engine.touch_account(account_id)
                result.preset_detected = preset.bank_id
                return result

            # 2. Auto-detect from header + sample data (covers new / unknown banks)
            auto_map = _auto_detect_columns(preview["headers"], preview["sample_rows"])
            if auto_map is not None:
                engine.save_column_map(account["bank_name"], auto_map)
                result = parse_and_import(raw, engine, account_id, auto_map, category)
                engine.touch_account(account_id)
                result.preset_detected = "auto"
                return result

            # 3. Manual mapping — return preview so the UI can ask the user
            return preview

        # Explicit map supplied or saved map found — use it
        if column_map is not None:
            engine.save_column_map(account["bank_name"], column_map)

        result = parse_and_import(raw, engine, account_id, effective_map, category)
        engine.touch_account(account_id)
        return result
