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
"""
from __future__ import annotations

SHEET_ID_META_KEY = "sheet_id"


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
