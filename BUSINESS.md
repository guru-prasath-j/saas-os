# Business Entities — Ledger + Compliance

Lets the user register any number of side businesses (e.g. "KMD Production",
"MJVR Investo") through a form, each getting the same generic two-tab
structure — Ledger and Compliance — with **zero new code required per
business added**. Modeled on the MCP connector pattern
(`amy/connectors/mcp.py` + `amy/saas/routers/mcp_connectors.py`): register a
plain data row, get fully generic behavior.

## Data model

All tables live in the per-user `finance.db` (`amy/finance/engine.py`).

```sql
business_entities (
    id, name, pan, gstin, constitution, registration_state,
    financial_year, tax_regime, holds_depreciable_assets,
    tracking_closeness, created_at
)
ledger_entries (
    id, business_entity_id, date, amount, description, category,
    source_event_id, source_document, confidence, posted_by,
    audit_status, created_at
)
compliance_suggestions (
    id, business_entity_id, ledger_entry_id, source_event_id,
    suggestion_type, reasoning, rate_used, citation, ca_disclaimer,
    routed_sensitive, created_at
)
rate_table (
    id, rate_type, key, value, effective_from, effective_to,
    source_note, updated_at
)
```

**Provenance:** every `ledger_entries` / `compliance_suggestions` row carries
a `source_event_id` — the id returned by `EventStore.emit()` at the moment
the row was created. `source_event_id` is `NOT NULL` on both tables: no
entry exists without a source event. This is the first schema-enforced
provenance link in the codebase — prior tables (e.g. `transactions.source`)
only store a free-text label, not an event reference. Because events live in
`collab.db` and ledger/compliance rows live in `finance.db` (separate SQLite
files, same pattern used throughout this app), it's an application-level FK,
not a DB-level one.

`business_entities` deliberately has **no `user_id` column** — every other
table in `finance.db` omits it too, since tenancy is the per-user DB file
itself (one `finance.db` per user).

## Add-business form

| Field | Column | Drives |
|---|---|---|
| Legal name | `name` | display, citations |
| PAN / GSTIN | `pan`, `gstin` | Compliance sensitivity routing (below) |
| Constitution | `constitution` | proprietorship / partnership / llp / company |
| Registration state, financial year, tax regime | — | context shown to the Compliance LLM |
| Holds depreciable assets | `holds_depreciable_assets` | whether depreciation suggestions apply |
| Tracking closeness | `tracking_closeness` | **close** → Auditor runs, lower auto-post confidence bar (0.6); **loose** → Auditor never runs, higher bar (0.85), more entries held for manual review |

## Ledger tab

**Accountant** (`amy/finance/business/accountant.py`) — takes an uploaded
document (spreadsheet or PDF; format varies per business), extracts
structured entries (date, amount, description, category, confidence) via a
single batch LLM call, mirroring the same pattern used for Gmail statement
enrichment. Each accepted entry is posted with a `source_event_id`. Entries
below the entity's auto-post confidence threshold are still posted, not
dropped, but are flagged for review. **Screenshots/photographed logs are not
yet supported** (no vision-LLM step exists in this codebase) — upload a
PDF/CSV/XLS instead; a v2 follow-up would add this.

The raw document text (not just individual entries) is scanned for
GSTIN/PAN before this extraction call — real invoices routinely print the
business's own GSTIN in a header/footer, so the same sensitivity rule used
in Compliance (below) applies here too, forcing the whole document through
the local-only path whenever it's present.

**Auditor** (`amy/finance/business/auditor.py`) — a read-only fidelity check
of already-posted entries against the source document (mismatched totals,
amounts not found in the source). It is a check against the source
document, not a second-opinion review — there is only one writer. Runs only
when `tracking_closeness == 'close'`.

**Known limitation:** the total-mismatch check is a rule-based heuristic
(sum of ledger amounts vs. numbers found in the source text), not an LLM
reasoning pass. It can false-positive on invoices that print a taxable
value, tax amount, and grand total as three separate numbers for one line
item (e.g. "Rs 60000 + GST Rs 10800 = Total Rs 70800") — the per-entry
"amount not found in source" check is the more reliable signal; treat a
lone `total_mismatch` issue as advisory, not a confirmed error.

One upload feeds the Ledger tab only. The Compliance tab (below) reuses
those entries automatically — no second upload.

## Compliance tab

Pipeline, run on demand over ledger entries that don't yet have a suggestion:

1. **Route by sensitivity** — entries whose own description matches a
   GSTIN/PAN pattern (`amy/finance/business/sensitivity.py`) go through the
   existing local-only Ollama path (`LLMRouter.pick(sensitive=True)` in
   `amy/llm.py`), called one entry at a time (local models track multi-item
   batch indices unreliably); everything else uses the normal batched
   cascade. This is scoped to the entry's own text, not the entity's PAN/GSTIN
   on file — the whole source document is separately sensitivity-checked at
   ingestion time (accountant.py), so this step isn't the only privacy gate,
   and treating every entry of any GST-registered business as permanently
   sensitive would force 100% of entries through the slower local path.
2. **Look up current rates** — GST slabs and depreciation blocks are read
   from `rate_table` (`amy/finance/business/rates.py`), never recalled from
   LLM training data. Seeded once with a small starter set; refreshed by
   editing rows (`PATCH /api/business/rates/{id}`), not an automated fetcher.
3. **Classify & calculate** — the LLM reasons over the entry plus the
   retrieved rate to produce a specific suggestion, grounded in the given
   rate table.
4. **Suggestion shown** — every suggestion displays its reasoning, cites the
   source ledger entry/document, and carries a persistent disclaimer.

**Amy never files or submits anything to any tax authority — suggestions
only, always confirm with your CA.**

## Automation roadmap (not built — Features 1-3 only in this pass)

| Enhancement | Build order | Depends on | Reuses |
|---|---|---|---|
| Cross-entity drift check | 1 | Features 1-3 | `custodial.py`'s `run_validation()` shape, generalized across entities |
| Recurring-entity pattern detection | 2 | (1) | `subscription_detect.py`'s candidate-filter → batch-LLM-call → confidence shape, applied to `ledger_entries` |
| Filing-period rollup | 3 | (2) | pure aggregation over `compliance_suggestions`, no new LLM calls |
| CA handoff export | 4 | (3) | formatting layer over the rollup — remains an export, never a filing |

Explicit stop-and-confirm gate: Feature 4 was not started in this pass;
sign-off is required before any of it is built.

## Routes

```
# Entities
POST    /api/business/entities                              create (the add-business form)
GET     /api/business/entities                               list
GET     /api/business/entities/{entity_id}                    get
PATCH   /api/business/entities/{entity_id}                    update
DELETE  /api/business/entities/{entity_id}                    delete

# Ledger (Accountant)
POST    /api/business/entities/{entity_id}/ledger/upload      multipart file -> extract -> auto-post
GET     /api/business/entities/{entity_id}/ledger              list posted entries
PATCH   /api/business/entities/{entity_id}/ledger/{entry_id}   manual correction
DELETE  /api/business/entities/{entity_id}/ledger/{entry_id}

# Auditor
POST    /api/business/entities/{entity_id}/ledger/audit        run_audit() -- 400 if tracking_closeness != 'close'

# Compliance
POST    /api/business/entities/{entity_id}/compliance/run      generate_suggestions() over unprocessed ledger entries
GET     /api/business/entities/{entity_id}/compliance           list suggestions

# Rate table (maintenance)
GET     /api/business/rates
PATCH   /api/business/rates/{rate_id}
```
