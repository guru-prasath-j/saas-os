# CLAUDE.md — Amy PersonalOS

Fast-load context. Read before touching code.

## Run

```bash
python -m uvicorn amy.saas.app:app --host 0.0.0.0 --port 8849
# Kill: Get-NetTCPConnection -LocalPort 8849 | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

## What It Is

FastAPI + single-page frontend. SQLite-per-user. One active user (`usergithub02@gmail.com`, uid `86878242670f411f87183bd5c20a5533`).

Primary: **Finance CFO** — CSV/XLS/PDF import, Gmail sync, budgets, subscriptions, investments, custodial accounts.

## Layout

```
amy/
  config.py              env vars (.env.personal → .env, override=False, first wins)
  llm.py                 LLMRouter: nvidia→openai→groq→ollama→template
  context.py             ContextModule: rolling event window for LLM injection
  vault_watcher.py       VaultWatcher: mtime-poll, emits vault.note_edited
  finance/
    engine.py            FinanceEngine: SQLite wrapper
    categorizer.py       Rule-based categorizer (instant, no API cost)
    afford.py            "Can I afford this?" logic
    budget_suggest.py    LLM-backed budget cap suggestions (income+spend+location)
    subscription_detect.py  Recurring charge detector (rule pre-filter → LLM confirm)
    custodial.py         Custodial account logic (SBI-style, never pollutes income)
    custodial_sheets.py  Google Sheets disbursement export
    sync/
      csv_import.py      CSV/XLS/XLSX (auto-detect cols, HDFC HTML-as-XLS fix)
      pdf_import.py      pdfplumber fast path → NVIDIA LLM fallback
      gmail_import.py    Gmail sync: 3-pass (parse → NVIDIA enrich → dedup insert)
      bank_presets.py    Named presets (HDFC, ICICI, SBI…)
      gmail_sensor.py    GmailSensor extends Sensor, polls + emits finance.gmail_synced
  events/
    store.py             EventStore: persist to collab.db events table + pub/sub
    triggers.py          Default subscribers (goal, vault, all finance events)
  memory/writer.py       MemoryWriter: idempotent vault journaling (daily + atomic notes)
  knowledge_graph/store.py  GraphStore: typed nodes+edges, edge UPSERT with timestamps
  saas/
    app.py               FastAPI entry — all routers included
    db.py                SQLAlchemy users table (amy_saas.db)
    deps.py              current_user, _user_key(), _connector_dir()
    paths.py             saas_data/index/{uid}/
    routers/
      finance.py         ~60 finance endpoints (main active router, ~1300 lines)
      auth.py            JWT login, OpenAI key, private folder
      connectors.py      Google OAuth flow + disconnect
      [11 others: vault, knowledge, habits, events, memory, twin, intelligence, agents…]
    static/index.html    Entire frontend (~3000 lines, one file)
```

## Data Paths

| What | Path |
|---|---|
| User DB | `saas_data/amy_saas.db` |
| Finance DB | `saas_data/index/86878242670f411f87183bd5c20a5533/finance.db` |
| Google token | `saas_data/index/86878242670f411f87183bd5c20a5533/connectors/google_token.json` |

## Finance DB Tables

`transactions` · `accounts` · `budgets` · `subscriptions` · `investments` · `income_sources` · `bank_column_maps`

`accounts.account_type`: `savings` | `current` | `credit_card` | `investment` | `custodial`
— custodial accounts are excluded from income/spend calculations.

## Finance API Routes

```
# Transactions
POST/GET/DELETE   /api/finance/transactions
DELETE            /api/finance/transactions/{tid}
POST              /api/finance/transactions/auto-categorize
PATCH             /api/finance/transactions/{tid}/category
GET               /api/finance/duplicates
POST              /api/finance/duplicates/resolve
DELETE            /api/finance/duplicates/auto

# Overview / forecast
GET               /api/finance/overview
GET               /api/finance/forecast/cashflow

# Accounts
POST/GET          /api/finance/accounts
GET/PATCH/DELETE  /api/finance/accounts/{aid}
GET               /api/finance/accounts/{aid}/transactions
POST              /api/finance/accounts/{aid}/preview/csv    # parse only, no save
POST              /api/finance/accounts/{aid}/preview/pdf
POST              /api/finance/accounts/{aid}/upload/csv     # parse + save
POST              /api/finance/accounts/{aid}/upload/pdf
POST              /api/finance/accounts/{aid}/upload/investments/csv
POST              /api/finance/accounts/{aid}/column-map

# Gmail / AA sync
POST              /api/finance/sync/gmail                    # global (all savings accounts)
POST              /api/finance/accounts/{aid}/sync/gmail     # per-account (legacy)
GET               /api/finance/gmail/scope-status
POST              /api/finance/accounts/{aid}/sync/aa
GET               /api/finance/accounts/{aid}/sync/aa/status

# Budgets
POST/GET          /api/finance/budgets
POST              /api/finance/budgets/suggestions           # LLM-backed suggestions
DELETE            /api/finance/budgets/{category}

# Subscriptions
POST/GET          /api/finance/subscriptions
GET               /api/finance/subscriptions/insights
POST              /api/finance/subscriptions/suggestions     # detect from transactions
PATCH/DELETE      /api/finance/subscriptions/{sid}

# Investments
POST/GET          /api/finance/investments
PATCH/DELETE      /api/finance/investments/{iid}

# Income
POST/GET          /api/finance/income
DELETE            /api/finance/income/{sid}

# Utilities
POST              /api/finance/afford
GET               /api/finance/goals
POST              /api/finance/calendar/sync
GET               /api/finance/bank-presets
GET               /api/finance/column-maps

# Custodial
POST              /api/finance/custodial/{account_id}/beneficiaries
GET               /api/finance/custodial/{account_id}/beneficiaries
GET               /api/finance/custodial/{account_id}/next-cycle-prefill
GET               /api/finance/custodial/{account_id}/validate
POST              /api/finance/custodial/{account_id}/disburse
POST              /api/finance/custodial/{account_id}/disburse/{transaction_id}/retry-sheet
POST              /api/finance/custodial/{account_id}/sheet            # link existing Sheet (URL/ID → meta.sheet_id)
GET               /api/finance/custodial/{account_id}/sheet/analyze    # read-only preview of tabs/rows
POST              /api/finance/custodial/{account_id}/sheet/import     # bootstrap beneficiaries + history (deduped)
POST              /api/finance/custodial/{account_id}/screenshot/parse # OCR + parse UPI/NEFT screenshot → prefilled log
GET               /api/finance/custodial/{account_id}/suggestions      # Gmail-synced debits fuzzy-matched to beneficiaries
POST              /api/finance/custodial/{account_id}/suggestions/{tid}/confirm  # claim existing debit (no duplicate txn)
POST              /api/finance/custodial/{account_id}/precheck         # soft anomaly warnings before confirm
```

Custodial AI layer: `amy/finance/custodial_ai.py` — regex/stats first, LLM
only as rescue and ALWAYS `sensitive=True` (local Ollama). Smart prefill
(median of last 3 cycles + trend note) rides on `next-cycle-prefill`;
cycle-close writes a vault note (`09_Memory/Custodial Cycle - …`) + deduped
notification via `_maybe_close_cycle()`; `/api/ask` intercepts custodial
questions (beneficiary-name tokens) in `vault.py:_try_custodial_answer` and
answers from the ledger, never vault notes.

## Business Entities

Register any side business via a form — Ledger (Accountant/Auditor) +
Compliance suggestions, zero new code per business. Full design: `BUSINESS.md`.

- Data lives in `finance.db`: `business_entities`, `ledger_entries`,
  `compliance_suggestions`, `rate_table` (`amy/finance/engine.py`).
- Business logic: `amy/finance/business/` (entities, accountant, auditor,
  compliance, sensitivity, rates).
- Routes: `amy/saas/routers/business.py`, prefix `/api/business/...`.
- GSTIN/PAN routing: `amy/finance/business/sensitivity.py` extends the same
  `LLMRouter.pick(sensitive=True)` local-only rule used for SBI/Sathish Appa
  — matched entries force Ollama-only, never a second routing mechanism.

```
POST/GET          /api/business/entities
GET/PATCH/DELETE  /api/business/entities/{entity_id}
POST/GET          /api/business/entities/{entity_id}/ledger
PATCH/DELETE      /api/business/entities/{entity_id}/ledger/{entry_id}
POST              /api/business/entities/{entity_id}/ledger/upload
POST              /api/business/entities/{entity_id}/ledger/audit
POST/GET          /api/business/entities/{entity_id}/compliance
POST              /api/business/entities/{entity_id}/compliance/run
GET/PATCH         /api/business/rates[/{rate_id}]
```

## Event System

```python
# EventStore (collab.db > events table) + in-process pub/sub
from amy.events.store import EventStore, FINANCE_GMAIL_SYNCED, ...
es = EventStore(cdb)
es.emit("finance.csv_imported", {"bank_name": ..., "imported": n}, source="finance")

# Finance router uses fire-and-forget helper — never breaks routes
def _emit_fin(user, event_type, payload): ...

# Event types emitted:
finance.transaction_added / csv_imported / pdf_imported / gmail_synced
finance.budget_set / subscription_added / investment_added / income_added
finance.ledger_entry_posted / ledger_audited / compliance_suggested
business.entity_created
vault.note_edited
goal.created / goal.completed / capture.added / digest.generated
```

## LLM Routing

Provider order: `AMY_PROVIDER_ORDER=nvidia,openai,groq,ollama` (env var).
- Sensitive data → Ollama only
- Gmail enrich / budget suggest → NVIDIA (batch, single call)
- `use_global_keys=True` required in finance routes

## Auth

JWT Bearer. `api()` JS helper adds it. `current_user` dep on all routes.
OAuth redirect: `{base_url}/api/connectors/google/callback` — must match Google Console exactly.

## Import Flows

**CSV/XLS:** preview → confirm. Column detection: saved map → bank preset → auto-detect → manual mapping UI. XLS magic: `\xD0\xCF` → xlrd, `PK` → openpyxl, HTML text → `_html_table_to_csv()`.

**PDF:** pdfplumber (line-based → text-based) → `_merge_split_rows()` → `_read_pdf_as_text()` → NVIDIA LLM if 0 rows.

**Gmail (3-pass):** `_DECLINED_RE` at message level → regex parse → LLM extract → `_enrich_with_llm()` (NVIDIA batch) → dedup insert. CC auto-routes to credit_card account.

## Known Quirks

1. `.env.personal` loads first (override=False) — new vars go to `.env` unless you want them shadowed.
2. HDFC XLS is real OLE binary (xlrd). `_html_table_to_csv()` is for HTML-disguised-as-XLS only.
3. `_find_col()` iterates headers in document order — never use set iteration.
4. Dr/Cr same column → `_auto_detect_columns` promotes it to `type_col`.
5. `_DECLINED_RE` checked at MESSAGE level before both regex and LLM — do not move it inside `_try_regex_parse()`.
6. Restart uvicorn after adding routes — new routes don't appear on hot-reload.
7. `parse_csv_preview_only` uses magic byte check, not extension — no XLS re-convert needed.
8. FastAPI route order: exact paths before parameterized (`/auto-categorize` before `/{tid}`).
9. Custodial accounts excluded from income/spend — `account_type='custodial'` is the flag.
10. `tracking_closeness` gates both Auditor execution and Accountant auto-post threshold on a business entity — check this before assuming the Auditor ran.
11. Image/screenshot ledger uploads are not yet supported — convert to PDF/CSV first (see `BUSINESS.md`).

## Common Pattern

```python
@router.get("/api/finance/something")
def my_route(user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        return {"data": fe.conn.execute("SELECT ...").fetchall()}
    finally:
        fe.close()
```
