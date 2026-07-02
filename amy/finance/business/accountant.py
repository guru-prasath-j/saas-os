"""Accountant: turn an uploaded business document (spreadsheet or PDF — format
varies per business) into structured ledger entries via a single batch LLM
call, mirroring amy/finance/sync/gmail_import.py's _enrich_with_llm shape
(candidates -> one prompt -> parsed JSON, degrade gracefully on failure).

Screenshots / photographed logs (images) are out of scope for v1 — no
vision-LLM step exists anywhere in this codebase yet. extract_ledger_entries()
raises UnsupportedDocumentFormat for image uploads; the router turns that
into a 400 telling the user to convert to PDF/CSV.
"""
from __future__ import annotations

import csv
import io
import json
import re

from .sensitivity import is_sensitive

_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "heic", "heif"}
_IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")


class UnsupportedDocumentFormat(ValueError):
    pass


_EXTRACT_SYSTEM = """You extract ledger entries from a small/informal business's \
records (a spreadsheet export, a photographed log transcribed to text, or a PDF \
statement — format varies per business). Return ONLY a JSON array, no markdown \
fences, no explanation. Each element must have exactly these keys:
  "date"        : ISO date string "YYYY-MM-DD" (best guess if the source is ambiguous)
  "amount"      : signed float — positive for money in, negative for money out
  "description" : short description of the entry (1-10 words)
  "category"    : a short category label, or "Uncategorized" if unclear
  "confidence"  : float 0-1 — how confident you are this is a real, correctly
                  parsed entry (lower it for ambiguous rows, guessed dates, or
                  unclear amounts)

IMPORTANT — Indian number formatting: amounts are commonly written in the
Indian lakh/crore digit-grouping system, e.g. "1,20,000.00" means ONE LAKH
TWENTY THOUSAND = 120000, NOT 12000 or 1.2. "70,800.00" means 70800. Strip
commas and read the full digit sequence left to right — do not assume
Western thousands-grouping (which groups digits in 3s throughout); Indian
grouping is 2 digits after the first 3 from the right. Getting this wrong
silently drops a digit, which is worse than a low confidence score — if
unsure of the digit grouping, lower confidence instead of guessing the
magnitude.

If no entries are found, return [].
"""

_USER_PROMPT = """Extract all ledger entries from the following business document text.
Return a JSON array only.

--- DOCUMENT TEXT ---
{text}
--- END ---
"""


def _detect_format(raw: bytes, filename: str) -> str:
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    if ext in _IMAGE_EXTS or raw[:4] in _IMAGE_MAGIC or raw[:3] == b"\xff\xd8\xff":
        return "image"
    if raw[:4] == b"%PDF" or ext == "pdf":
        return "pdf"
    if raw[:2] == b"PK" or ext == "xlsx":
        return "xlsx"
    if raw[:4] == b"\xd0\xcf\x11\xe0" or ext == "xls":
        return "xls"
    return "csv"


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _spreadsheet_to_text(raw: bytes, filename: str, fmt: str) -> str:
    if fmt in ("xls", "xlsx"):
        from ..sync.csv_import import _xls_to_csv
        raw = _xls_to_csv(raw, filename)
    text = _decode(raw)
    reader = csv.reader(io.StringIO(text))
    lines = ["\t".join(cell.strip() for cell in row) for row in reader if any(row)]
    return "\n".join(lines)


def _pdf_to_text(raw: bytes) -> str:
    from ..sync.pdf_import import _extract_text
    return _extract_text(raw)


def auto_post_threshold(tracking_closeness: str) -> float:
    """Minimum LLM confidence required to auto-post without a review flag.
    Closely-tracked entities get a lower bar (more auto-posts); loosely-tracked
    entities hold more for manual review."""
    return 0.6 if tracking_closeness == "close" else 0.85


def extract_ledger_entries(raw: bytes, filename: str, llm) -> list[dict]:
    """Return a list of {date, amount, description, category, confidence} dicts.
    Raises UnsupportedDocumentFormat for image uploads."""
    fmt = _detect_format(raw, filename)
    if fmt == "image":
        raise UnsupportedDocumentFormat(
            "Screenshot/photo uploads aren't supported yet — "
            "please convert this document to PDF or CSV/XLS first.")

    text = _pdf_to_text(raw) if fmt == "pdf" else _spreadsheet_to_text(raw, filename, fmt)
    if not text.strip() or llm is None:
        return []

    prompt = _USER_PROMPT.format(text=text[:12000])
    # Real invoices/statements commonly print the business's own GSTIN/PAN in
    # a header or footer — the raw document text goes to the LLM here, not
    # just individual extracted entries, so it must be sensitivity-checked
    # too (same rule Compliance applies per-entry in compliance.py).
    sensitive = is_sensitive(text)

    entries = None
    # Local (Ollama) models are noticeably less reliable at strict JSON
    # formatting than the cloud path, and GSTIN/PAN-bearing documents are
    # forced through it — retry once on a malformed/empty response before
    # giving up, rather than silently dropping the whole batch to zero.
    for _attempt in range(2):
        try:
            raw_resp, _ = llm.generate(_EXTRACT_SYSTEM, prompt, sensitive=sensitive)
            raw_resp = re.sub(r"```(?:json)?", "", raw_resp).strip()
            start, end = raw_resp.find("["), raw_resp.rfind("]")
            if start == -1 or end == -1:
                continue
            entries = json.loads(raw_resp[start:end + 1])
            break
        except Exception:
            continue
    if not entries:
        return []

    result = []
    for e in entries:
        if not isinstance(e, dict) or e.get("date") is None or e.get("amount") is None:
            continue
        description = str(e.get("description") or "")[:200]
        amount = float(e["amount"])
        category = e.get("category") or "Uncategorized"
        if category == "Uncategorized":
            # The extraction LLM (especially the local model, forced for
            # GSTIN/PAN-bearing documents) often leaves this blank — fall
            # back to the same fast, free, rule-based categorizer already
            # used everywhere else in Finance CFO instead of leaving every
            # row "Uncategorized".
            from ..categorizer import categorize as _kw_categorize
            guess = _kw_categorize(description, amount)
            # categorizer.py's keyword rules were written for PERSONAL
            # finance, where "salary"/"refund"/"cashback" almost always mean
            # money coming IN — it doesn't check the amount's sign. For a
            # business ledger the same words often describe the business
            # PAYING OUT (e.g. "salary payout to staff" is an expense, not
            # income). Only trust an Income-implying guess when the amount
            # actually is incoming; otherwise leave it Uncategorized rather
            # than confidently mislabel an expense as income.
            category = "Uncategorized" if guess == "Income" and amount < 0 else guess
        result.append({
            "date": str(e["date"])[:10],
            "amount": amount,
            "description": description,
            "category": category,
            "confidence": float(e.get("confidence", 0.5)),
        })
    return result
