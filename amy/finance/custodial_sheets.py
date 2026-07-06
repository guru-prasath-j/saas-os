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
_REFILL_RE = re.compile(r"refill|credit|deposit|received|top[- ]?up", re.I)


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
            rows, skipped = [], 0
            for raw in vr.get("values", []):
                raw = list(raw) + [""] * (6 - len(raw))
                date = _parse_date(raw[0])
                amount = _parse_amount(raw[3])
                if date is None or not amount:
                    if any(str(c).strip() for c in raw):
                        skipped += 1
                    continue
                kind = "refill" if _REFILL_RE.search(f"{raw[1]} {raw[4]}") else "disbursement"
                rows.append({
                    "date": date,
                    "mode": str(raw[2]).strip(),
                    "amount": abs(amount),
                    "category": str(raw[4]).strip() or "Custodial Disbursement",
                    "notes": str(raw[5]).strip(),
                    "kind": kind,
                })
            rows.sort(key=lambda r: r["date"])
            tabs.append({"tab": name, "rows": rows, "skipped": skipped})
        return {"ok": True, "sheet_title": title, "tabs": tabs}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def append_disbursement_row(creds, account: dict, beneficiary: dict,
                            date: str, mode: str, amount: float,
                            category: str, notes: str) -> dict:
    """
    Appends one row to the beneficiary's tab. Returns
    {"ok": True, "tab": ...} or {"ok": False, "error": ...} — callers should
    NOT roll back the transaction/event on failure; offer a retry instead.
    """
    if creds is None:
        return {"ok": False, "error": "Google not linked — connect it in Account settings"}

    sheet_id = (account.get("meta") or {}).get(SHEET_ID_META_KEY)
    if not sheet_id:
        return {"ok": False, "error": "no sheet_id configured on this custodial account"}

    tab = beneficiary.get("sheet_tab") or beneficiary["name"]

    try:
        from googleapiclient.discovery import build
        svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"'{tab}'!A:F",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [[date, "Disbursement", mode, amount, category, notes]]},
        ).execute()
        return {"ok": True, "tab": tab}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
