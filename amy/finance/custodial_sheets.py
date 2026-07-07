"""Writes custodial disbursement rows into the user's existing Google Sheet
(e.g. "SBI Account Management" — one tab per beneficiary, a running Balance
cell). Never creates or restructures the sheet: only appends rows to the tab
matching a beneficiary, using columns A-F exactly as the user's sheet is
already laid out (Date, Account Type, Mode, Amount, Category,
Notes/Screenshots). Never touches the Balance column/cell and never uses
batchUpdate (which is what would alter structure/formatting) — the ledger
(transactions + events) is the source of truth; this is a mirrored,
human-referenceable copy the user already relies on.

Refills are intentionally NOT written here (per user decision) — they stay
internal, tracked only via custodial.refilled events and the balance calc.

Bootstrap: fetch_sheet_data() READS the same sheet (one tab per beneficiary,
columns A-F) so an already-manually-maintained sheet can seed beneficiaries
and history into the ledger — read-only, never modifies the sheet.
"""
from __future__ import annotations

import datetime as _dt
import re

SHEET_ID_META_KEY = "sheet_id"

# dd/mm first — these sheets are Indian-locale
_DATE_FMTS = (
    "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y",
    "%d %b %Y", "%d-%b-%Y", "%d %B %Y", "%b %d, %Y", "%m/%d/%Y",
)
# Explicit-only: "Credit" in a beneficiary tab's Category column means
# "credited TO the beneficiary" (a disbursement), so credit/deposit/received
# must NOT flag a refill. Real refills come from the master tab's Credit
# column or an explicit refill/top-up label.
_REFILL_RE = re.compile(r"\brefill\b|top[- ]?up", re.I)

# Header-cell name → canonical field. Tabs use different layouts
# (Date|Account Type|Mode|Amount|Category|Notes vs Date|Holder|Amount|Balance
# vs the master log Date|Account Type|Credit|Debit|Payment Method|Balance),
# so columns are mapped by header text; Balance columns are never amounts.
_HDR_MAP = {
    "date": "date",
    "amount": "amount",
    "credit": "credit",
    "debit": "debit",
    "mode": "mode",
    "payment method": "mode",
    "category": "category",
    "notes": "notes",
    "note": "notes",
    "notes/screenshots": "notes",
    "account type": "party",
    "holder": "party",
}


def _detect_header(raw_rows: list) -> tuple[int | None, dict]:
    """Find the header row in the first 10 rows and map columns by name.
    Falls back to the legacy fixed A-F layout when no header is found."""
    for i, row in enumerate(raw_rows[:10]):
        cells = [str(c).strip().lower() for c in row]
        if "date" not in cells:
            continue
        colmap: dict = {}
        for j, c in enumerate(cells):
            field = _HDR_MAP.get(c)
            if field and field not in colmap:
                colmap[field] = j
        if "date" in colmap and ({"amount", "credit", "debit"} & colmap.keys()):
            return i, colmap
    return None, {"date": 0, "party": 1, "mode": 2, "amount": 3,
                  "category": 4, "notes": 5}


def extract_sheet_id(text: str) -> str | None:
    """Accepts a full Sheets URL or a bare spreadsheet ID."""
    text = (text or "").strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", text):
        return text
    return None


def _parse_date(v) -> str | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        # Sheets serial date (days since 1899-12-30), in case a cell comes raw
        if 20000 < v < 80000:
            return (_dt.date(1899, 12, 30) + _dt.timedelta(days=int(v))).isoformat()
        return None
    s = str(v or "").strip()
    if not s:
        return None
    for fmt in _DATE_FMTS:
        try:
            return _dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_amount(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v or "").strip()
    s = re.sub(r"(?i)[₹,\s]|rs\.?|inr", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def fetch_sheet_data(creds, sheet_id: str) -> dict:
    """
    Read-only fetch of every tab's A:F rows, parsed into
    {date, mode, amount, category, notes, kind} dicts (kind: disbursement |
    refill, keyword-detected from the Account Type / Category columns).
    Unparseable non-empty rows (headers, balance cells) are counted as
    skipped. Returns {"ok": False, "error": ...} on any API failure.
    """
    if creds is None:
        return {"ok": False, "error": "Google not linked — connect it in Account settings"}
    if not sheet_id:
        return {"ok": False, "error": "no sheet linked to this custodial account yet"}
    try:
        from googleapiclient.discovery import build
        svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        meta = svc.spreadsheets().get(
            spreadsheetId=sheet_id,
            fields="properties.title,sheets.properties.title").execute()
        title = (meta.get("properties") or {}).get("title", "")
        tab_names = [s["properties"]["title"] for s in meta.get("sheets", [])]
        if not tab_names:
            return {"ok": True, "sheet_title": title, "tabs": []}

        res = svc.spreadsheets().values().batchGet(
            spreadsheetId=sheet_id,
            ranges=[f"'{t}'!A1:F2000" for t in tab_names],
            valueRenderOption="UNFORMATTED_VALUE",
            dateTimeRenderOption="FORMATTED_STRING").execute()

        tabs = []
        for name, vr in zip(tab_names, res.get("valueRanges", [])):
            raw_rows = vr.get("values", [])
            hdr_i, cm = _detect_header(raw_rows)
            start = hdr_i + 1 if hdr_i is not None else 0
            # master layout = the account's own money-in/money-out log
            # (Credit + Debit columns). Its credits are the real refills; its
            # debits duplicate the per-beneficiary tabs and are never imported.
            layout = "master" if ("credit" in cm and "debit" in cm) else "simple"

            def cell(row, field):
                j = cm.get(field)
                return row[j] if j is not None and j < len(row) else ""

            rows, skipped = [], 0
            debits_skipped, debit_total = 0, 0.0
            for raw in raw_rows[start:]:
                date = _parse_date(cell(raw, "date"))
                if date is None:
                    if any(str(c).strip() for c in raw):
                        skipped += 1
                    continue
                party = str(cell(raw, "party")).strip()
                base = {
                    "date": date,
                    "mode": str(cell(raw, "mode")).strip(),
                    "category": str(cell(raw, "category")).strip(),
                    "notes": str(cell(raw, "notes")).strip(),
                    "party": party,
                }
                if layout == "master":
                    credit = _parse_amount(cell(raw, "credit"))
                    debit = _parse_amount(cell(raw, "debit"))
                    if credit and credit > 0:
                        rows.append({**base, "amount": abs(credit),
                                     "category": base["category"] or "Refill",
                                     "kind": "refill"})
                    if debit and debit > 0:
                        # account outflow: the import step decides — parties
                        # covered by their own tab (Eswari, Sumathi) are
                        # skipped there; the rest (e.g. Guru IB) are real
                        # disbursements that exist only in this log
                        rows.append({**base, "amount": abs(debit),
                                     "category": base["category"] or "Custodial Disbursement",
                                     "kind": "account_debit"})
                        debits_skipped += 1
                        debit_total += abs(debit)
                    if not credit and not debit:
                        skipped += 1
                    continue
                amount = _parse_amount(cell(raw, "amount"))
                if not amount:
                    skipped += 1   # header noise, notes-only and balance rows
                    continue
                kind = ("refill" if _REFILL_RE.search(f"{party} {base['category']}")
                        else "disbursement")
                rows.append({**base, "amount": abs(amount),
                             "category": base["category"] or "Custodial Disbursement",
                             "kind": kind})
            rows.sort(key=lambda r: r["date"])
            tabs.append({"tab": name, "rows": rows, "skipped": skipped,
                         "layout": layout, "debits_skipped": debits_skipped,
                         "debit_total": round(debit_total, 2)})
        return {"ok": True, "sheet_title": title, "tabs": tabs}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def append_disbursement_row(creds, account: dict, beneficiary: dict,
                            date: str, mode: str, amount: float,
                            category: str, notes: str,
                            part: str | None = None) -> dict:
    """
    Appends one row to the beneficiary's tab. For split beneficiaries the
    Account Type column carries "<name> <part>" (e.g. "Eswari Personal"),
    matching the user's existing tab convention. Returns
    {"ok": True, "tab": ...} or {"ok": False, "error": ...} — callers should
    NOT roll back the transaction/event on failure; offer a retry instead.
    """
    if creds is None:
        return {"ok": False, "error": "Google not linked — connect it in Account settings"}

    sheet_id = (account.get("meta") or {}).get(SHEET_ID_META_KEY)
    if not sheet_id:
        return {"ok": False, "error": "no sheet_id configured on this custodial account"}

    tab = beneficiary.get("sheet_tab") or beneficiary["name"]
    account_type = f"{beneficiary['name']} {part}".strip() if part else "Disbursement"

    try:
        from googleapiclient.discovery import build
        svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"'{tab}'!A:F",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [[date, account_type, mode, amount, category, notes]]},
        ).execute()
        return {"ok": True, "tab": tab}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
