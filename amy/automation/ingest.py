"""Hybrid statement auto-ingest (Phase 2).

Watches Gmail for bank-statement attachments (PDF/CSV/XLS/XLSX) and parses
them with the existing import pipeline. HYBRID decision rule:

  confident  → auto-import (tier 1: done + notification)
               confidence = account matched unambiguously AND rows parsed AND
               the column mapping came from a saved map / bank preset (CSV),
               or the deterministic pdfplumber path (PDF).
  uncertain  → parked in the Approval Inbox (tier 2) with the fully parsed
               rows in the payload — approving imports them, nothing is
               re-parsed. LLM-proposed column maps (self-healing mapping)
               always take this path, and get persisted to bank_column_maps
               on approval so the same format is confident next time.

Every attachment is recorded in ingested_attachments so it is processed once.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re

from .executors import JobCtx, submit_action

_EXT_RE = re.compile(r"\.(pdf|csv|xls|xlsx)$", re.IGNORECASE)
_MAX_ATTACHMENTS_PER_RUN = 5

GMAIL_STATEMENT_QUERY = (
    'has:attachment newer_than:14d '
    '(statement OR "account statement" OR e-statement OR estatement)'
)


# ---------------------------------------------------------------------------
# Self-healing column mapping (LLM proposal, validated against real headers)
# ---------------------------------------------------------------------------

_MAP_SYSTEM = (
    "You map bank statement CSV headers to a fixed schema. Given the headers "
    "and sample rows, return ONLY a JSON object with keys: date, description, "
    "debit, credit, amount, type, date_format. Values MUST be exact header "
    "strings from the list (or null). Use debit+credit for separate columns, "
    "amount (+type if there is a Dr/Cr indicator column) for a single signed "
    "column. date_format is a Python strptime format like %d/%m/%Y, or null."
)


def propose_column_map_llm(headers: list[str], sample_rows: list[dict], llm) -> dict | None:
    """Ask the LLM for a column map; validate every value against headers."""
    if llm is None or not headers:
        return None
    prompt = (f"Headers: {json.dumps(headers)}\n"
              f"Sample rows: {json.dumps(sample_rows[:3], default=str)}")
    try:
        raw, _ = llm.generate(_MAP_SYSTEM, prompt, sensitive=False)
        raw = re.sub(r"```(?:json)?", "", raw).strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return None
        m = json.loads(raw[start:end + 1])
    except Exception:
        return None
    cmap = {}
    for key in ("date", "description", "debit", "credit", "amount", "type"):
        v = m.get(key)
        cmap[key] = v if isinstance(v, str) and v in headers else None
    fmt = m.get("date_format")
    if isinstance(fmt, str) and "%" in fmt:
        cmap["date_format"] = fmt
    if not cmap["date"] or not cmap["description"]:
        return None
    if not (cmap["debit"] or cmap["credit"] or cmap["amount"]):
        return None
    return cmap


# ---------------------------------------------------------------------------
# Account matching
# ---------------------------------------------------------------------------

def _match_account(accounts: list[dict], text: str) -> tuple[dict | None, bool]:
    """Match a statement to a user account by bank name tokens.
    Returns (account, ambiguous). Custodial accounts are eligible too — their
    refills just never count as income (engine handles that)."""
    text_l = text.lower()
    matches = []
    for a in accounts:
        bank = (a.get("bank_name") or "").lower()
        tokens = [t for t in re.split(r"[^a-z]+", bank) if len(t) >= 3]
        if tokens and all(t in text_l for t in tokens):
            matches.append(a)
    if len(matches) == 1:
        return matches[0], False
    if len(matches) > 1:
        # prefer a savings/current account when the bank has several
        primary = [a for a in matches
                   if a.get("account_type") in ("savings", "current", None, "")]
        if len(primary) == 1:
            return primary[0], False
        return matches[0], True
    if len(accounts) == 1:
        return accounts[0], False
    return None, True


# ---------------------------------------------------------------------------
# Parsing one attachment → (transactions, mapping_source, column_map)
# ---------------------------------------------------------------------------

def _parse_attachment(fe, account: dict, filename: str, raw: bytes, llm):
    """Returns (txns, mapping_source, column_map). mapping_source is one of
    saved_map | preset | auto | llm | pdfplumber | pdf_llm | none."""
    account_type = (account or {}).get("account_type", "")

    if filename.lower().endswith(".pdf"):
        from ..finance.sync.pdf_import import (
            _parse_pdf_pdfplumber, parse_pdf_preview_only, PasswordRequired)
        try:
            deterministic = _parse_pdf_pdfplumber(raw)
        except PasswordRequired:
            return [], "password_required", None
        except Exception:
            deterministic = []
        txns = parse_pdf_preview_only(raw, llm=llm, account_type=account_type)
        return txns, ("pdfplumber" if deterministic else "pdf_llm"), None

    # CSV / XLS / XLSX
    from ..finance.sync.csv_import import (
        _xls_to_csv, preview_csv, _auto_detect_columns, parse_csv_preview_only)
    from ..finance.sync.bank_presets import detect_preset

    if raw[:2] in (b"PK", b"\xd0\xcf") or filename.lower().endswith((".xls", ".xlsx")):
        try:
            raw = _xls_to_csv(raw, filename)
        except Exception:
            return [], "none", None

    prev = preview_csv(raw)
    headers, sample_rows = prev["headers"], prev["sample_rows"]
    if not headers:
        return [], "none", None

    bank_name = (account or {}).get("bank_name") or ""
    column_map, mapping_source = None, "none"

    if bank_name:
        column_map = fe.get_column_map(bank_name)
        if column_map:
            mapping_source = "saved_map"
    if column_map is None:
        preset = detect_preset(headers)
        if preset is not None:
            column_map, mapping_source = dict(preset.column_map), "preset"
    if column_map is None:
        column_map = _auto_detect_columns(headers, sample_rows)
        if column_map:
            mapping_source = "auto"
    if column_map is None:
        column_map = propose_column_map_llm(headers, sample_rows, llm)
        if column_map:
            mapping_source = "llm"
    if column_map is None:
        return [], "none", None

    txns = parse_csv_preview_only(raw, column_map, filename, account_type)
    return txns, mapping_source, column_map


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

def _walk_attachments(payload: dict):
    """Yield (filename, attachment_id) for every named attachment part."""
    for part in payload.get("parts", []) or []:
        fname = part.get("filename") or ""
        body = part.get("body", {}) or {}
        if fname and body.get("attachmentId"):
            yield fname, body["attachmentId"]
        yield from _walk_attachments(part)


def gmail_statement_ingest(ctx: JobCtx) -> dict:
    creds = ctx.google_creds()
    if creds is None:
        return {"skipped": "no google token"}
    try:
        from googleapiclient.discovery import build
    except ImportError:
        return {"skipped": "google-api-python-client not installed"}

    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    res = svc.users().messages().list(
        userId="me", maxResults=20, q=GMAIL_STATEMENT_QUERY).execute()
    messages = res.get("messages", [])

    fe = ctx.open_finance()
    summary = {"scanned": 0, "auto_imported": 0, "pending_approval": 0,
               "skipped": 0, "errors": []}
    try:
        accounts = [a for a in fe.list_accounts()
                    if a.get("account_type") != "investment"]
        if not accounts:
            return {"skipped": "no accounts configured"}

        processed = 0
        for msg_ref in messages:
            if processed >= _MAX_ATTACHMENTS_PER_RUN:
                break
            try:
                msg = svc.users().messages().get(
                    userId="me", id=msg_ref["id"], format="full").execute()
            except Exception as exc:
                summary["errors"].append(f"{msg_ref['id']}: {exc}")
                continue
            payload = msg.get("payload", {})
            hdrs = {h["name"]: h["value"] for h in payload.get("headers", [])}
            subject, from_hdr = hdrs.get("Subject", ""), hdrs.get("From", "")

            for filename, att_id in _walk_attachments(payload):
                if processed >= _MAX_ATTACHMENTS_PER_RUN:
                    break
                if not _EXT_RE.search(filename):
                    continue
                if ctx.store.attachment_seen(msg_ref["id"], filename):
                    continue
                summary["scanned"] += 1
                try:
                    att = svc.users().messages().attachments().get(
                        userId="me", messageId=msg_ref["id"], id=att_id).execute()
                    raw = base64.urlsafe_b64decode(att["data"])
                except Exception as exc:
                    summary["errors"].append(f"{filename}: download failed — {exc}")
                    continue
                sha = hashlib.sha256(raw).hexdigest()

                account, ambiguous = _match_account(
                    accounts, f"{from_hdr} {subject} {filename}")
                if account is None:
                    ctx.store.mark_attachment(msg_ref["id"], filename, sha,
                                              "skipped", "no matching account")
                    summary["skipped"] += 1
                    continue

                try:
                    txns, mapping_source, column_map = _parse_attachment(
                        fe, account, filename, raw, ctx.llm)
                except Exception as exc:
                    ctx.store.mark_attachment(msg_ref["id"], filename, sha,
                                              "error", str(exc)[:300])
                    summary["errors"].append(f"{filename}: {exc}")
                    continue

                if not txns:
                    ctx.store.mark_attachment(msg_ref["id"], filename, sha,
                                              "skipped", f"no rows ({mapping_source})")
                    summary["skipped"] += 1
                    continue

                total_out = sum(abs(t["amount"]) for t in txns if t["amount"] < 0)
                total_in = sum(t["amount"] for t in txns if t["amount"] > 0)
                confident = (not ambiguous
                             and mapping_source in ("saved_map", "preset", "pdfplumber"))
                action_payload = {
                    "account_id": account["id"],
                    "bank_name": account.get("bank_name", ""),
                    "filename": filename,
                    "transactions": txns,
                    "column_map": column_map,
                    # persist LLM/auto-detected maps on approval → next
                    # statement of this format imports automatically
                    "save_column_map": mapping_source in ("llm", "auto"),
                }
                body = (f"{len(txns)} transactions parsed from {filename} "
                        f"→ {account.get('nickname') or account.get('bank_name')} "
                        f"(₹{total_in:,.0f} in / ₹{total_out:,.0f} out, "
                        f"mapping: {mapping_source}"
                        f"{', account match ambiguous' if ambiguous else ''})")
                r = submit_action(
                    ctx,
                    tier=1 if confident else 2,
                    action_type="import_statement",
                    title=(f"Statement auto-imported: {filename}" if confident
                           else f"Review statement import: {filename}"),
                    body=body,
                    payload=action_payload,
                    source="gmail_statement_ingest",
                    dedup_key=f"ingest_{msg_ref['id']}_{filename}")
                if r["status"] in ("auto_executed", "pending"):
                    status = "auto_imported" if confident else "pending_approval"
                    summary[status] += 1
                else:   # duplicate / failed
                    status = r["status"]
                    summary["skipped"] += 1
                ctx.store.mark_attachment(msg_ref["id"], filename, sha, status,
                                          f"{len(txns)} rows, {mapping_source}")
                processed += 1
    finally:
        fe.close()
    return summary
