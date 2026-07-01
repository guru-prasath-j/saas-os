# Prompt: Automate the SBI custodial account disbursement flow

Paste this into Claude Code (or a fresh chat) inside the `saas-os` repo.

---

## Context (read first, don't skip)

There is a separate SBI account, refilled periodically by the user's uncle,
that exists purely to hold and forward money to a fixed set of beneficiaries.
It is not the user's own money — the account is custodial/in-trust. Every
month-end, the user manually sends NEFT/UPI transfers to each beneficiary and
then hand-enters each transfer into a Google Sheet ("SBI Account Management")
with columns: `Date, Account Type, Mode, Amount, Category, Notes/Screenshots`,
plus a running `Balance` cell and one tab per beneficiary.

The current beneficiary/split pattern observed in the sheet:
- **Eswari** — split into two transfers each cycle: a personal portion (UPI/Gpay)
  and an MJVR portion (Account Transfer)
- **MJVR** — direct
- **VJPN**, **VJPN 2**, **Sumathi** — direct

This is a single-user account with no other party ever writing to it — do
**not** build separate Accountant/Auditor roles for this flow. That role
split is reserved for multi-party business entities (KMD Production, MJVR
Investo) elsewhere in the project, not this ledger. Keep this as one flow with
built-in validation checks, not a second persona.

The core guardrail already established for this project applies directly
here and must not be relaxed: **the app never moves money or initiates a
transfer.** It detects, prompts, logs, and validates — the user always sends
the actual NEFT/UPI transfer themselves.

---

## Step 0 — Mandatory audit before writing any code

1. Read the current finance module's ingestion path end to end — specifically
   how bank statement/email parsing currently works (`amy/finance/`,
   including the Gmail 3-pass sync), and confirm whether it currently
   distinguishes account types or treats all inflows/outflows the same way.
2. Read the existing Google OAuth connector (`amy/saas/routers/connectors.py`
   or wherever it actually lives per the repo tree) to confirm what scopes are
   already granted, and whether Sheets API write access needs a new scope
   added to the existing Google connection or a separate one.
3. Confirm where `events.emit()` is and isn't currently called in the finance
   path (per the earlier audit, Gmail sync bypasses it) — this new flow must
   not repeat that gap; every refill and every disbursement must emit an
   event.
4. Check the Flutter app's existing share-intent / gallery capture code
   (`flutter_app/`) to confirm the actual mechanism available for attaching a
   screenshot to a specific record, rather than assuming an API that isn't
   there.
5. Write out a short plan: new data model, new/reused API endpoints, which
   existing modules are touched vs. left alone, and the UI surface (mobile
   and/or dashboard) this appears in. **Stop and get explicit sign-off before
   writing any code.**

---

## Data model

A new custodial account concept, scoped to finance:

- `custodial_account`: id, user_id, label ("SBI — Uncle"), custodian_name,
  current_balance (derived, not stored as truth — recomputed from events)
- `beneficiary`: id, custodial_account_id, name (Eswari, MJVR, VJPN, VJPN 2,
  Sumathi), default_split (e.g. Eswari → [personal, mjvr_transfer])
- `disbursement_template`: last-used amounts per beneficiary/split, used to
  prefill the next cycle — not a fixed schedule, just a starting point the
  user edits
- Every refill and every disbursement is an **event** first
  (`custodial.refilled`, `custodial.disbursed`), with the ledger row and
  Sheets row both derived from that event — not written independently of it.
  This keeps the event log as the single source of truth, consistent with
  the rest of the project's event-driven memory pattern.

## Flow to build

1. **Refill detection** — extend the existing bank-alert parsing to recognize
   this specific account and emit `custodial.refilled` automatically when a
   credit lands. No manual entry for this half.
2. **Month-end nudge** — a notification, timed relative to the last
   disbursement cycle (not a hardcoded calendar date — infer cadence from
   history), prefilled with the last cycle's beneficiary split as an editable
   starting point.
3. **Manual transfer, then one-tap log** — the user sends each NEFT/UPI
   transfer themselves; confirming it in the app writes:
   - the event (`custodial.disbursed`, with beneficiary, split type, amount,
     mode)
   - a row to the actual Google Sheet via the Sheets API (same sheet the
     user already has — do not create a new one)
   - both from the same confirmation action, not two separate steps
4. **Screenshot attach** — reuse the existing Flutter share-intent capture to
   attach a UPI/NEFT confirmation screenshot to the specific disbursement
   event, replacing the manual paste-into-Notes-column step.
5. **Balance** — computed as `sum(refill events) - sum(disbursement events)`,
   shown wherever the account appears, not maintained as a manually-edited
   cell.

## Built-in validation checks (not a separate role)

Run these as checks against the user's own entries, surfaced as flags, not as
review from a second persona:

- Does a split beneficiary's parts (e.g. Eswari personal + Eswari MJVR) sum to
  the expected total for that cycle?
- Did all usual beneficiaries get logged this cycle? Flag any that were
  skipped.
- Has a refill not arrived by the date a disbursement would normally be due?

## Explicit non-goals

- No automated transfer initiation, ever.
- No Accountant/Auditor role split for this account.
- Do not replace the Google Sheet — write to it, keep it as the
  user-facing record they already share/reference.

## Acceptance criteria

- [ ] Refill detection emits an event automatically from existing bank-alert
      parsing, no manual step
- [ ] Month-end nudge prefills from the last cycle's actual split
- [ ] Confirming a sent transfer writes both the event and the Sheets row in
      one action
- [ ] Screenshot capture attaches to the correct disbursement event
- [ ] Balance is computed from events, never hand-edited
- [ ] All three validation checks fire correctly against a test cycle with a
      deliberately introduced gap (skipped beneficiary, mismatched split,
      late refill)
- [ ] Existing Gmail sync and other finance flows are unaffected unless the
      Step 0 audit found a specific reason to touch them
