"""Investment portfolio CSV importer.

Imports investment holdings from a CSV (mutual funds, stocks, etc.).
Column map schema:
  {
    "name":          <header for fund/stock name>,
    "type":          <header for asset class, e.g. "Equity", "Debt"> OR null,
    "current_value": <header for current market value>,
    "cost_basis":    <header for invested / purchase amount>          OR null
  }

UPSERT semantics: if an investment with the same name already exists, its
current_value (and optionally cost_basis) is updated.  This models a portfolio
snapshot — uploading the same statement twice is idempotent.
"""
from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING

from . import SyncProvider, SyncResult

if TYPE_CHECKING:
    from ..engine import FinanceEngine

# Saved under bank_column_maps with this key prefix so it doesn't clash with
# bank statement maps.
_MAP_KEY_PREFIX = "investment:"


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("latin-1", errors="replace")


def _parse_float(raw: str | None) -> float | None:
    if not raw:
        return None
    cleaned = (str(raw).strip()
               .replace(",", "")
               .replace("₹", "")
               .replace("Rs.", "")
               .replace("Rs ", ""))
    if not cleaned or cleaned in ("-", "N/A", "NA"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def preview_csv(raw: bytes, max_rows: int = 3) -> dict:
    text = _decode(raw)
    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])
    rows = []
    for i, row in enumerate(reader):
        if i >= max_rows:
            break
        rows.append(dict(row))
    return {"headers": headers, "sample_rows": rows, "needs_mapping": True}


def parse_and_import(
    raw: bytes,
    engine: "FinanceEngine",
    column_map: dict,
) -> SyncResult:
    text = _decode(raw)
    reader = csv.DictReader(io.StringIO(text))

    name_col = column_map.get("name", "")
    type_col = column_map.get("type")
    cv_col = column_map.get("current_value", "")
    cb_col = column_map.get("cost_basis")

    result = SyncResult()

    for row_num, row in enumerate(reader, start=2):
        name = row.get(name_col, "").strip() if name_col else ""
        if not name:
            result.errors.append(f"Row {row_num}: empty name, skipped")
            continue

        inv_type = row.get(type_col, "General").strip() if type_col else "General"
        current_value = _parse_float(row.get(cv_col, "") if cv_col else None)
        cost_basis = _parse_float(row.get(cb_col, "") if cb_col else None)

        if current_value is None:
            result.errors.append(f"Row {row_num}: invalid current_value for '{name}'")
            continue

        # UPSERT: update if name already exists
        existing = engine.conn.execute(
            "SELECT id FROM investments WHERE name=? LIMIT 1",
            (name,)).fetchone()

        if existing:
            upd: dict = {"current_value": current_value}
            if cost_basis is not None:
                upd["cost_basis"] = cost_basis
            if inv_type and inv_type != "General":
                upd["type"] = inv_type
            engine.update_investment(existing["id"], **upd)
            result.skipped += 1   # counts as "seen but not new"
            result.transactions.append(
                {"action": "updated", "name": name, "current_value": current_value})
        else:
            iid = engine.add_investment(
                inv_type or "General",
                name,
                current_value=current_value,
                cost_basis=cost_basis or 0.0,
            )
            result.imported += 1
            result.transactions.append(
                {"action": "added", "id": iid, "name": name,
                 "current_value": current_value})

    return result


class InvestmentCSVProvider(SyncProvider):
    method = "investment_csv"

    def available(self) -> bool:
        return True

    def import_from_bytes(
        self,
        raw: bytes,
        engine: "FinanceEngine",
        account_id: str | None = None,   # optional, for storing the map
        column_map: dict | None = None,
    ) -> "SyncResult | dict":
        """
        If column_map is None and no saved map exists for this account:
            returns preview dict.
        Otherwise:
            imports and returns SyncResult.
        """
        effective_map = column_map

        # Try to look up saved map by account_id
        if effective_map is None and account_id:
            key = f"{_MAP_KEY_PREFIX}{account_id}"
            effective_map = engine.get_column_map(key)

        if effective_map is None:
            return preview_csv(raw)

        # Persist for next time
        if column_map is not None and account_id:
            key = f"{_MAP_KEY_PREFIX}{account_id}"
            engine.save_column_map(key, column_map)

        return parse_and_import(raw, engine, effective_map)
