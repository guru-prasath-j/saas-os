# Prompt: Dynamic business entities — Ledger + Compliance, with docs

Paste this into Claude Code (or a fresh chat) inside the `saas-os` repo.

---

## Context (read first, don't skip)

Today, business tracking for entities like KMD Production and MJVR Investo
would have to be hardcoded per entity. This replaces that with a generic,
dynamic "business entity" concept — add any number of businesses through the
UI, each getting the same two-tab structure, with no new code required per
business added.

---

## Step 0 — Mandatory audit before writing any code

1. Read the current finance module end to end (`amy/finance/`), including
   how it currently handles categorization, recurring detection, and the
   Gmail 3-pass ingestion — this feature reuses those ingestion patterns for
   business documents, it does not reinvent them.
2. Read `amy/knowledge_graph/`, `amy/memory/writer.py`, and `amy/events/store.py`
   to confirm the current event-sourcing and provenance pattern, since every
   ledger entry and every compliance suggestion in this feature must be
   event-sourced and provenance-linked to its source document, consistent
   with the rest of the project.
3. Confirm where `amy/llm.py`'s `LLMRouter` currently enforces the
   sensitive-data-to-local-Ollama rule (used today for SBI/Sathish Appa) —
   this feature extends that same rule to GSTIN/PAN data; read the existing
   mechanism before adding a second one.
4. Read the existing Finance CFO page's tab-bar implementation (frontend) to
   confirm the exact pattern to replicate for a business entity's two tabs.
5. Write out a short plan: data model, new endpoints, which existing modules
   are touched vs. left alone, and where the new documentation files will
   live. **Stop and get explicit sign-off before writing any code.**

---

## Feature 1 — Dynamic business entity

### Add-business form (asked once, at creation)
Two categories of questions in one form:

**Identity / compliance details:**
- Legal name, PAN, GSTIN (if registered)
- Business constitution — proprietorship / partnership / LLP / company
- Registration state
- Financial year, tax regime if known
- Entity type — for depreciation purposes, does it hold property/assets

**Tracking-closeness (decides automation behavior, not just review depth):**
- "How closely do you track this business?" → closely-managed vs.
  mostly-recording-only. This single answer:
  - decides whether the Auditor pass runs on the ledger side
  - decides how much the Accountant auto-posts vs. holds for review
    (closely-tracked → higher auto-post confidence threshold; loosely-tracked
    → hold more for manual confirmation)

Submitting this form creates the entity and both its tabs. No code changes
should be required to add a fourth, fifth, or Nth business later — this is
the generic pattern from the earlier MCP connector work, applied here.

### Data model
- `business_entity`: id, user_id, name, pan, gstin, constitution,
  registration_state, financial_year, tracking_closeness, created_at
- Every ledger entry and every compliance suggestion references
  `business_entity_id` and a `source_event_id`, per the project's existing
  provenance pattern — no entry exists without a source event.

---

## Feature 2 — Ledger tab (Accountant + Auditor)

- **Accountant function**: takes an uploaded document (screenshot,
  spreadsheet, photographed log — format varies per business) and extracts
  structured entries (amount, date, description) via LLM, same pattern as
  the existing bank-statement multi-format handling. Posts as an event.
- **Auditor function**: read-only pass over posted entries, checking against
  the source document — flags mismatched totals, missing line items, or
  entries that don't reconcile with what was uploaded. This runs only when
  `tracking_closeness` indicates the user wants it; it is a fidelity check
  against the source document, not a review of a second person's work — there
  is only one writer.
- One upload feeds this tab only. Do not require a second upload for the
  Compliance tab — see Feature 3.

---

## Feature 3 — Compliance tab

Reuses the ledger entries from Feature 2 automatically. Pipeline:

1. **Route by sensitivity** — GSTIN/PAN-bearing entries go through the local
   Ollama path (extend the existing sensitive-data router, don't build a
   second one); everything else can use the normal cascade.
2. **Look up current rates** — GST rates, depreciation blocks, and
   thresholds are read from a maintained rate table, not recalled from LLM
   training data. Design this table to be refreshed periodically, not
   hardcoded once.
3. **Classify & calculate** — LLM reasons over the entry plus the retrieved
   rate to produce a specific suggestion (e.g. likely depreciation block and
   estimated amount for an asset; likely GST input credit eligibility for a
   purchase).
4. **Suggestion shown** — every suggestion displays its reasoning, cites the
   source entry/document, and carries a persistent "confirm with your CA"
   framing. Nothing in this pipeline files, submits, or claims anything with
   any tax authority — it only suggests.

---

## Feature 4 — Automation enhancements (build after 1–3 are working)

Build in this order, each depending on the previous:

1. **Cross-entity drift check** — a portfolio-level pass (not duplicated per
   entity) that flags things only visible across multiple businesses, e.g.
   "N entities exist but only some had activity logged this period."
2. **Recurring-entity pattern detection** — after a few months of history,
   infer each entity's typical cash-flow rhythm automatically (e.g. "deposit
   roughly every 30 days") instead of requiring a manually configured
   schedule, and use it to time the month-end nudge per entity.
3. **Filing-period rollup** — accumulate every Compliance suggestion
   generated in a period into one running summary per entity, rather than
   leaving them scattered as individual flags.
4. **CA handoff export** — generate a structured document (numbers,
   reasoning, linked source documents) per entity per period from the
   rollup in (3), ready to hand to a CA. This remains an export, never a
   filing.

`tracking_closeness` from Feature 1 should also gate Accountant auto-post
confidence, per the design already specified there — implement that gating
as part of Feature 2, not as a separate enhancement.

---

## Documentation requirements — do not skip

1. **Create a new `BUSINESS.md`** at the repo root, documenting: the
   business entity data model, the add-business form fields and what each
   drives (identity vs. tracking-closeness), the Ledger tab's
   Accountant/Auditor behavior, the Compliance tab pipeline (with the
   explicit "suggests, never files" boundary stated plainly), and the four
   automation enhancements with their build order and dependencies.
2. **Update `CLAUDE.md`** — add this feature to whatever onboarding-for-Claude-Code
   section already exists there (following the style of existing entries
   like the config load-order and route-ordering gotchas), including: where
   `business_entity` data lives, the sensitive-data routing extension for
   GSTIN/PAN, and a pointer to `BUSINESS.md` for the full design.
3. **Update `README.md`** — add Business/Compliance to the list of what the
   project does, consistent with how Finance CFO mode is already described
   there. Keep it brief; point to `BUSINESS.md` for detail rather than
   duplicating it in the main README.
4. Do not let `BUSINESS.md` drift from the other module docs already in the
   repo (`SAAS.md`, `PRODUCT.md`, `ROADMAP.md` etc.) — read their existing
   tone/format first and match it rather than inventing a new doc style.

---

## Explicit non-goals

- No automated filing or submission to any tax authority, ever.
- No hardcoded per-business logic — everything must work for an Nth business
  added purely through the form, with zero new code.

## Acceptance criteria

- [ ] Adding a business via the form requires no code changes for future
      businesses
- [ ] Ledger tab posts entries with correct provenance links; Auditor only
      runs when `tracking_closeness` calls for it
- [ ] Compliance tab pipeline runs automatically off existing ledger entries,
      no second upload required
- [ ] GSTIN/PAN-bearing data is confirmed routed through the local-only path
- [ ] Every compliance suggestion shows reasoning, a source citation, and the
      "confirm with your CA" framing
- [ ] `BUSINESS.md` created, `CLAUDE.md` and `README.md` updated, all
      consistent in tone with existing docs
- [ ] Existing Finance CFO tabs are unaffected unless the Step 0 audit found
      a specific reason to touch them

Stop after Features 1–3 are working and confirmed before starting Feature 4.
