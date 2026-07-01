"""Multi-bank CSV format preset library.

Maps common Indian bank statement CSV header patterns to column maps so users
don't need to manually configure the mapping for known banks.

Each preset is a dict with:
  "name"        — display name for the bank
  "headers"     — set of exact header strings that uniquely identify this bank's CSV
  "column_map"  — the column_map dict to use (matches CSVImportProvider's schema)
  "notes"       — optional human-readable comment about the format

Auto-detection via detect_preset(headers) works by matching the intersection of
expected headers against actual headers. An exact-match bank wins outright; a
partial match (≥ min_match_ratio) is returned as a "probable" preset.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BankPreset:
    name: str
    bank_id: str       # key for bank_column_maps table
    required_headers: set[str]  # headers that MUST all be present
    column_map: dict
    notes: str = ""


# ---------------------------------------------------------------------------
# Preset definitions
# Each "required_headers" set uses case-normalised strings (lowercased in detection).
# ---------------------------------------------------------------------------

PRESETS: list[BankPreset] = [

    BankPreset(
        name="HDFC Bank",
        bank_id="HDFC",
        required_headers={"date", "narration", "value dt", "withdrawal amt.", "deposit amt.", "closing balance"},
        column_map={
            "date":        "Date",
            "description": "Narration",
            "debit":       "Withdrawal Amt.",
            "credit":      "Deposit Amt.",
            "date_format": "%d/%m/%y",
        },
        notes="HDFC Net Banking statement CSV — separate Withdrawal/Deposit columns, DD/MM/YY dates.",
    ),

    BankPreset(
        name="ICICI Bank",
        bank_id="ICICI",
        required_headers={"transaction date", "value date", "description", "ref no./cheque no.", "debit", "credit", "balance"},
        column_map={
            "date":        "Transaction Date",
            "description": "Description",
            "debit":       "Debit",
            "credit":      "Credit",
            "date_format": "%d/%m/%Y",
        },
        notes="ICICI Bank account statement — separate Debit/Credit columns, DD/MM/YYYY dates.",
    ),

    BankPreset(
        name="SBI (State Bank of India)",
        bank_id="SBI",
        required_headers={"txn date", "value date", "description", "ref no./cheque no.", "debit", "credit", "balance"},
        column_map={
            "date":        "Txn Date",
            "description": "Description",
            "debit":       "Debit",
            "credit":      "Credit",
            "date_format": "%d %b %Y",
        },
        notes="SBI e-statement CSV — separate Debit/Credit, dates like '01 Jun 2025'.",
    ),

    BankPreset(
        name="Axis Bank",
        bank_id="AXIS",
        required_headers={"tran. id", "value date", "cheque no", "particulars", "debit", "credit", "bal"},
        column_map={
            "date":        "Value Date",
            "description": "Particulars",
            "debit":       "Debit",
            "credit":      "Credit",
            "date_format": "%d-%m-%Y",
        },
        notes="Axis Bank statement — separate Debit/Credit, DD-MM-YYYY dates.",
    ),

    BankPreset(
        name="Kotak Mahindra Bank",
        bank_id="KOTAK",
        required_headers={"transaction date", "value date", "particulars", "cheque number", "amount", "dr/cr", "balance"},
        column_map={
            "date":        "Transaction Date",
            "description": "Particulars",
            "amount":      "Amount",
            "type":        "Dr/Cr",
            "date_format": "%d-%m-%Y",
        },
        notes="Kotak Bank statement — single Amount column with Dr/Cr type indicator.",
    ),

    BankPreset(
        name="IndusInd Bank",
        bank_id="INDUSIND",
        required_headers={"date", "transaction details", "chq. no.", "value date", "withdrawal amount (inr)", "deposit amount (inr)", "balance (inr)"},
        column_map={
            "date":        "Date",
            "description": "Transaction Details",
            "debit":       "Withdrawal Amount (INR)",
            "credit":      "Deposit Amount (INR)",
            "date_format": "%d/%m/%Y",
        },
        notes="IndusInd Bank Net Banking statement.",
    ),

    BankPreset(
        name="Yes Bank",
        bank_id="YESBANK",
        required_headers={"date", "description", "cheque/ref. no.", "debit (inr)", "credit (inr)", "balance (inr)"},
        column_map={
            "date":        "Date",
            "description": "Description",
            "debit":       "Debit (INR)",
            "credit":      "Credit (INR)",
            "date_format": "%d/%m/%Y",
        },
        notes="Yes Bank statement — INR labelled columns.",
    ),

    BankPreset(
        name="IDFC First Bank",
        bank_id="IDFC",
        required_headers={"date", "remarks", "withdrawal (dr)", "deposit (cr)", "balance"},
        column_map={
            "date":        "Date",
            "description": "Remarks",
            "debit":       "Withdrawal (Dr)",
            "credit":      "Deposit (Cr)",
            "date_format": "%d-%m-%Y",
        },
        notes="IDFC First Bank statement.",
    ),
]


# ---------------------------------------------------------------------------
# Detection logic
# ---------------------------------------------------------------------------

def _normalise(headers: list[str]) -> set[str]:
    return {h.strip().lower() for h in headers}


def detect_preset(headers: list[str]) -> BankPreset | None:
    """Return the best-matching BankPreset for a set of CSV headers, or None.

    Matching rules (in priority order):
      1. All required headers present → exact match, return immediately.
      2. ≥ 80% of required headers present → return as best partial match.
      3. No match → return None (caller will show manual mapping UI).
    """
    normalised = _normalise(headers)
    best_preset: BankPreset | None = None
    best_ratio = 0.0

    for preset in PRESETS:
        req = {h.lower() for h in preset.required_headers}
        matched = req & normalised
        ratio = len(matched) / len(req) if req else 0.0

        if ratio == 1.0:
            return preset  # exact — no need to look further

        if ratio > best_ratio:
            best_ratio = ratio
            best_preset = preset

    if best_ratio >= 0.8:
        return best_preset
    return None


def get_preset(bank_id: str) -> BankPreset | None:
    """Look up a preset by bank_id (case-insensitive)."""
    bid = bank_id.upper()
    for p in PRESETS:
        if p.bank_id.upper() == bid:
            return p
    return None


def list_presets() -> list[dict]:
    """Return a summary of all available presets (for API/UI display)."""
    return [
        {"bank_id": p.bank_id, "name": p.name,
         "required_headers": sorted(p.required_headers), "notes": p.notes}
        for p in PRESETS
    ]
