# CLAUDE.md — PersonalOS / Amy

Fast-load context for any Claude instance working in this repo. Read this before touching code.

---

## What This Is

**Amy** is a self-hosted personal AI operating system — a FastAPI SaaS app + single-page frontend. One user (`usergithub02@gmail.com`), multi-tenant codebase, SQLite-per-user data model.

Primary active feature: **Finance CFO mode** — CSV/XLS/PDF import, Gmail sync, auto-categorization, budgets, subscriptions, investments.

---

## Run the Server

```bash
python -m uvicorn amy.saas.app:app --host 0.0.0.0 --port 8849
```

Kill existing: `Get-NetTCPConnection -LocalPort 8849 | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }`

---

## Architecture at a Glance

```
amy/
  config.py            — all env vars, dotenv loader (.env.personal → .env, override=False)
  llm.py               — LLMRouter: nvidia → openai → groq → ollama → template
  finance/
    engine.py          — FinanceEngine: SQLite wrapper (transactions, accounts, budgets…)
    categorizer.py     — Rule-based categorizer + LLM batch fallback (account-type aware)
    afford.py          — "Can I afford this?" logic
    subscription_detect.py — Detect recurring charges from transactions (rules + 1 LLM call)
    budget_suggest.py  — Suggest per-category budget caps from income + spend + location
    sync/
      csv_import.py    — CSV/XLS/XLSX import (auto-detect columns, HDFC HTML-as-XLS fix)
      pdf_import.py    — PDF import: pdfplumber fast path → NVIDIA LLM fallback
      gmail_import.py  — Gmail sync: 3-pass (parse → NVIDIA enrich → dedup insert)
      bank_presets.py  — Named bank column-map presets (HDFC, ICICI, SBI…)
  saas/
    app.py             — FastAPI app entry, includes all 14 routers
    db.py              — SQLAlchemy users table (amy_saas.db)
    deps.py            — current_user dep, _user_key(), _connector_dir()
    paths.py           — saas_data/index/{uid}/ per-user paths
    routers/
      finance.py       — ~50 finance API endpoints (the main active router)
      auth.py          — JWT login, OpenAI key storage, private folder settings
      connectors.py    — Google OAuth flow
      [11 other routers — vault, knowledge, habits, events, memory, twin…]
    static/
      index.html       — ENTIRE frontend: one ~2500-line HTML+JS+CSS file
```

---

## Data Locations (Production)

| What | Path |
|------|------|
| User DB | `saas_data/amy_saas.db` (users table) |
| Finance DB | `saas_data/index/86878242670f411f87183bd5c20a5533/finance.db` |
| Google token | `saas_data/index/86878242670f411f87183bd5c20a5533/connectors/google_token.json` |
| User email | `usergithub02@gmail.com` → uid `86878242670f411f87183bd5c20a5533` |

---

## Finance DB Schema (SQLite)

Tables in `finance.db`:
- `transactions` — id, date, amount, category, merchant, source, notes, account_id
- `accounts` — id, nickname, bank_name, account_type, last_sync
- `budgets` — category, monthly_limit
- `subscriptions` — name, amount, billing_cycle, next_due, category
- `investments` — type, name, current_value, cost_basis
- `income_sources` — name, amount, frequency
- `bank_column_maps` — bank_name, column_map (JSON)

---

## Finance API Routes (all in `amy/saas/routers/finance.py`)

```
GET/POST  /api/finance/transactions            list / add / DELETE all (reset)
                                                 GET supports ?account_id= to filter by real account
DELETE    /api/finance/transactions/{tid}       delete one
POST      /api/finance/transactions/auto-categorize   rules + CC-Income recheck + LLM batch fallback
PATCH     /api/finance/transactions/{tid}/category

GET       /api/finance/overview                 summary card data
GET       /api/finance/forecast/cashflow

POST/GET/DELETE  /api/finance/accounts          CRUD
DELETE    /api/finance/accounts/{aid}           also deletes its transactions
GET/POST  /api/finance/accounts/{aid}/transactions

POST      /api/finance/accounts/{aid}/preview/csv   ← parse, NO save, returns [{date,desc,amount,category}]
POST      /api/finance/accounts/{aid}/preview/pdf   ← parse, NO save
POST      /api/finance/accounts/{aid}/upload/csv    ← parse + save
POST      /api/finance/accounts/{aid}/upload/pdf    ← parse + save
POST      /api/finance/accounts/{aid}/column-map    save manual column mapping

POST      /api/finance/sync/gmail              global sync ALL savings accounts (uses NVIDIA)
                                                 since= omitted on auto-poll → resumes per-account
                                                 from last_synced_at instead of assuming "today"
POST      /api/finance/accounts/{aid}/sync/gmail   per-account sync (legacy)
GET       /api/finance/gmail/scope-status

POST/GET/DELETE  /api/finance/budgets
POST      /api/finance/budgets/suggestions      LLM-backed suggestions from income+spend+location
POST/GET/PATCH/DELETE  /api/finance/subscriptions
POST      /api/finance/subscriptions/suggestions  detect recurring charges not yet tracked
POST/GET/PATCH/DELETE  /api/finance/investments
POST/GET/DELETE  /api/finance/income
POST      /api/finance/afford
GET       /api/finance/goals
POST      /api/finance/calendar/sync
GET       /api/finance/bank-presets
GET       /api/finance/column-maps

POST      /api/settings/location                set profile location (used by budget_suggest)
```

---

## LLM Routing (`amy/llm.py`)

```python
# Provider order from AMY_PROVIDER_ORDER env var (default: nvidia,openai,groq,ollama)
# In .env.personal: AMY_PROVIDER_ORDER=nvidia,openai,groq,ollama

class NvidiaLLM:    # model: nvidia/nemotron-3-ultra-550b-a55b, thinking mode, streaming
class OpenAILLM:    # model: gpt-4o-mini
class GroqLLM:      # model: llama-3.3-70b-versatile
class OllamaLLM:    # model: llama3.2, local only
class TemplateLLM:  # deterministic fallback, always works

# Usage in routers:
llm = LLMRouter(openai_api_key=_user_key(user), use_global_keys=True)
```

**Key rules:**
- Sensitive data → Ollama only (never sent to cloud)
- Gmail sync enrichment → NVIDIA (batch, single call per sync)
- `use_global_keys=True` in all finance routes (uses server-side NVIDIA key)
- **All cloud clients (Nvidia/OpenAI/Groq) have an explicit `timeout`** (75s Nvidia, 45s others) — the `openai`/`groq` SDKs default to a 10-minute timeout otherwise, which hangs the whole request if the endpoint is slow/overloaded. Never construct one of these clients without a timeout.

---

## Config & Environment (`amy/config.py`)

**Load order:** `.env.personal` first (override=False), then `.env`. First value wins.

Key vars (set in `.env` + `.env.personal`):
```
NVIDIA_API_KEY=nvapi-...           # primary LLM
AMY_PROVIDER_ORDER=nvidia,openai,groq,ollama
GROQ_API_KEY=...
OPENAI_API_KEY=...
OLLAMA_HOST=http://localhost:11434
AMY_OLLAMA_MODEL=llama3.2
PERSONALOS_MODE=personal
```

**Critical gotcha:** If you add a new env var, add it to BOTH `.env` AND `.env.personal` (or just `.env`). `.env.personal` loads first so it can shadow `.env`.

---

## Auth Pattern

- JWT Bearer token in `Authorization: Bearer <token>` header
- Frontend `api()` helper adds this automatically from `TOKEN` var
- All finance routes: `user: User = Depends(current_user)`
- `_user_key(user)` → per-user OpenAI API key (stored in saas.db)
- `_connector_dir(user)` → `saas_data/index/{uid}/connectors/`
- `users.location` (free-text country/city) — optional, set at signup or later via `POST /api/settings/location`. No IP geolocation (this app runs on localhost; a public-IP lookup would need an external API call and usually can't resolve a private/loopback address anyway). Powers `budget_suggest.py`'s cost-of-living localization.

---

## CSV/XLS Import Flow

```
File select (onchange) → previewImport(aid, 'csv')
  → POST /api/finance/accounts/{aid}/preview/csv   [parse only, no DB write]
  → shows table: date | description | amount | category
  → user clicks ✓ Import → confirmImport()
  → POST /api/finance/accounts/{aid}/upload/csv    [parse + dedup insert]
```

**Column detection priority:**
1. Saved column map for this bank (`bank_column_maps` table)
2. Named bank preset (`bank_presets.py`)
3. Auto-detect from headers + sample rows (`_auto_detect_columns`)
4. Return `needs_mapping: true` (manual mapping UI)

**XLS formats handled:**
- True OLE binary XLS (`\xD0\xCF\x11\xE0` magic) → xlrd
- ZIP-based XLSX (`PK` magic) → openpyxl
- HTML-as-XLS (HDFC export, starts with text) → `_html_table_to_csv()`

---

## PDF Import Flow

```
pdfplumber fast path (2 strategies: line-based, text-based)
  → _merge_split_rows()  [HDFC CC split rows]
  → if header row found but ≤1 txn parsed: _read_pdf_as_text() rescue
    [table can be legit but sparse while the real data is in an unstructured
    blob elsewhere in the same PDF — see Known Quirks #9]
  → if 0 rows total: NVIDIA LLM fallback (requires `pymupdf` — see Known Quirks #11)
```

**Column detection** in `_parse_pdf_pdfplumber` handles two statement layouts:
1. Separate Debit/Credit columns (`_PLB_DEBIT_WORDS`/`_PLB_CREDIT_WORDS`)
2. One combined "Amount" column with a trailing `Cr`/`Dr` marker (e.g. HDFC CC:
   `"875.00Cr"`, no space) — `_PLB_AMOUNT_WORDS` + `_split_combined_amount()` +
   `_CR_SUFFIX_RE`. **The Cr/Dr regex must NOT use a trailing `\b`** — a digit
   immediately followed by a letter (`00Cr`) has no word boundary between
   them, so `\bcr\b` silently fails to match. Anchor on end-of-string instead:
   `r"(?i)cr\.?\s*$"`.

---

## Gmail Sync Flow (3-pass)

```
Pass 1: for each email
  - _DECLINED_RE check at MESSAGE level (blocks both regex + LLM)
  - _try_regex_parse() → raw_txns  (tags row["source"] = "gmail"/"cc_gmail" itself)
  - if empty: extract_transactions_llm() → raw_txns, THEN manually tag
    row["source"] = "cc_gmail" if "credit card" in email else "gmail" —
    extract_transactions_llm() is shared with pdf_import.py and has no idea
    it's parsing an email, so its rows come back with no source key at all.
    Forgetting this tag means the row silently defaults to source="pdf" in
    parse_and_import_pdf(), AND the is_cc detection right after (which checks
    `source == "cc_gmail"`) never fires for LLM-fallback-extracted rows.
  - _detect_bank() tags bank name from sender domain

Pass 2: _enrich_with_llm()
  - keyword pre-categorize
  - single NVIDIA batch call for uncategorized/long descriptions
  - updates merchant name + category

Pass 3: dedup insert per account
  - CC transactions auto-route to credit_card account
```

**Global sync endpoint:** `POST /api/finance/sync/gmail` — syncs ALL savings/current accounts in one call. Auto-creates CC accounts per bank if needed.

---

## Categorizer (`amy/finance/categorizer.py`)

Rule-based first (instant, no API cost), with a batched LLM fallback for whatever rules can't resolve. Applied automatically on every import.

**Rule order:** keyword sets → CC bill → POS fee → ATM → personal UPI → fund transfer → insurance → govt → tech → food extras → fuel → retail → BharatPe → bank fee/tax (`_BANK_FEE_TAX_RE`) → positive amount=Income/EMI-Loan → Uncategorized

To add a merchant: add keyword to the appropriate set in `_RULES` (or `_RETAIL_RE`/`_BANK_FEE_TAX_RE` for the broader regex buckets).

**`categorize(merchant, amount, notes, account_type)` is account-type aware**: a positive amount on a `credit_card` account is a **bill payment** (`EMI/Loan`), not real income — only savings/current accounts default an unmatched credit to `Income`. Every call site (CSV/PDF preview + insert, `auto_categorize_all`) fetches `account_type` from the account and passes it through. Forgetting this at a new call site silently mislabels every CC payment as income (and would inflate `effective_monthly_income()` — see engine.py notes below).

**`categorize_batch_llm(candidates, llm)`** — one batched call (not one per transaction) for rows still `Uncategorized` after rules. Told explicitly to answer `"Uncategorized"` rather than guess. `auto_categorize_all(engine, llm=None)` runs rules first, collects the leftovers, and does one LLM pass over all of them together. It also **re-checks existing `Income`-tagged rows on credit-card accounts** (not just blank/Uncategorized ones), so a bug like the one above gets corrected retroactively the next time categorization runs, not just on new imports.

**GST/tax-split and bank-fee lines** (e.g. HDFC's own `SGST-VPS.../CGST-VPS...` reversal rows, `FINANCE CHARGES`) aren't real merchant purchases — `_BANK_FEE_TAX_RE` buckets them into `EMI/Loan` rather than leaving them `Uncategorized` forever or forcing them into a spending category that doesn't fit.

---

## Frontend (`amy/saas/static/index.html`)

Single file ~2500 lines. Key JS patterns:
```javascript
api(path, opts)          // authenticated fetch — always use this, never raw fetch()
jget(path)               // GET + .json()
jpost(path, body)        // POST JSON + .json()
TOKEN                    // JWT, set on login
_fmt(amount)             // format ₹ amount
loadFinTransactions()    // refresh transaction list
loadFinAccounts()        // refresh account list
```

**Gmail sync state:**
```javascript
_globalSyncTimer         // setInterval handle (null = off)
_globalSyncBusy          // bool, prevents overlapping syncs
startGlobalAutoSync() / stopGlobalAutoSync() / toggleGlobalAutoSync()
maybeAutoStartGmailSync()  // called from onLogin() — resumes auto-sync unless
                           // localStorage 'amy_auto_sync'==='off', so the user
                           // doesn't have to click "Auto" again every session
syncGmailAll(auto)       // auto=true: no since= (server resumes from
                         // last_synced_at); auto=false: 30days+500msgs explicit
```

**Transactions tab account filter:** `txAccFilter` is populated from the real `/api/finance/accounts` list (`_populateTxAccFilter()`), and `loadFinTransactions()` passes the selected account's real id as `?account_id=`. Do NOT go back to filtering client-side by guessing from `source`/text content (e.g. "does merchant text contain 'credit card'") — that was the original bug: transactions genuinely linked to the credit-card account but imported via PDF/CSV don't mention "credit card" anywhere in their text, so they'd be invisible to a text-heuristic filter.

**Subscriptions/Budget suggestion cards** (`loadSubSuggestions()`/`loadBudgetSuggestions()` in the Subscriptions/Budget panes): re-run fresh every time the tab opens, no persistence of dismissals — each suggestion row is editable (name/amount/cycle) before accepting.

**File preview state:**
```javascript
_stagedFiles[aid]        // {type, file} — set on file select, cleared on cancel/confirm
previewImport(aid,type)  // file onchange handler → calls preview endpoint
cancelImport(aid,type)   // clears file input + preview div
confirmImport(aid,type)  // calls uploadCSV/uploadPDF → commits to DB
```

---

## Common Patterns

**Add a new finance route:**
```python
# In amy/saas/routers/finance.py
@router.get("/api/finance/something")
def my_route(user: User = Depends(current_user)):
    fe = _finance_db(user)
    try:
        # use fe.conn.execute() for raw SQL or fe.method() for engine methods
        return {"data": ...}
    finally:
        fe.close()
```

**Add a new FinanceEngine method:**
- Add to `amy/finance/engine.py`
- All methods use `self.conn` (sqlite3.Connection)
- `self.touch_account(aid)` updates `last_sync` timestamp

**Route ordering rules (FastAPI):**
- Exact paths MUST be registered before parameterized paths with same prefix
- e.g. `DELETE /api/finance/transactions` before `DELETE /api/finance/transactions/{tid}`
- e.g. `POST /api/finance/transactions/auto-categorize` before `DELETE /api/finance/transactions/{tid}`

---

## Known Quirks

1. **`.env.personal` loads before `.env`** (override=False). New env vars must be added to the correct file — if `.env.personal` has it already set, `.env` won't override it.

2. **HDFC XLS files** are real OLE binary (not HTML). xlrd reads them. The `_html_table_to_csv()` fallback is for banks that export HTML with `.xls` extension.

3. **`_find_col()` uses header file order** (not set iteration). Always iterate headers in document order to avoid non-deterministic column selection.

4. **Dr/Cr collision**: if `_find_col` maps debit and credit to the same column, `_auto_detect_columns` promotes it to `type_col`.

5. **Declined payments**: `_DECLINED_RE` is checked at **message level** in `sync_gmail()` — BEFORE both regex parser and LLM fallback. Moving the check inside `_try_regex_parse()` is wrong (LLM fallback would still import them).

6. **Server restart required** after changing routes. Module-level code (regex, constants) reloads per-request, but new routes only appear after uvicorn restart.

7. **`use_global_keys=True`** is required in finance routes to access NVIDIA key. Per-user OpenAI keys are optional extras.

8. **`parse_csv_preview_only`** does NOT re-convert XLS (uses magic byte check, not extension). The endpoint already converts before calling it.

9. **A "header found" table can still be the wrong table.** Some bank PDF templates (e.g. HDFC's "duplicate statement") render a whole section as one bordered box with no internal grid lines — pdfplumber returns it as a single giant multi-line cell. Naive header detection (checking if a cell contains "date"/"desc" as a substring) can mistake that blob for the header row, since the embedded text contains those words too. Fixed by rejecting any candidate header row where a cell is oversized or contains `\n`, **and** by falling back to `_read_pdf_as_text()` whenever the table parse yields ≤1 transaction (a legit-but-nearly-empty header can exist on one page while the real data sits unstructured elsewhere).

10. **Footer/branding rows can repeat at every page boundary** (e.g. a GSTIN strip printed on every page). The PDF row-parsing loop used to `break` entirely on the first footer-like match — which works for a true end-of-statement footer, but kills all parsing once it hits a mid-document page-boundary artifact, silently dropping every transaction on later pages. Changed to `continue` (the existing date/description validity checks already reject genuine summary rows on their own, so this doesn't reopen the door to misparsing those).

11. **`pymupdf` (imported as `fitz`) is required for the PDF LLM fallback** (`_extract_text()` in `pdf_import.py`) but is easy to leave off `requirements.txt` since nothing imports it at module load time — the `import fitz` only happens inside the function, so a missing install fails silently (caught by a bare `except: pass`) with zero indication anything is wrong. Always verify it's actually importable, not just listed in requirements.

12. **Near-duplicate matching (`_is_near_duplicate` in pdf_import.py, shared by CSV/PDF/Gmail insert)** has two failure modes to watch for:
    - *Under-matching*: the same real purchase described differently across sources (e.g. Gmail vs a PDF statement) — `"Lifestyle"` vs `"Life Style International"` — won't share any `[A-Z]{3,}` token, so the original token-overlap check misses it. Fixed with `_norm_desc()` (alphanumeric-only, uppercased) + a substring-containment check, in addition to the token check.
    - *Over-matching*: two genuinely different line items that happen to share a reference number and generic words — e.g. an `SGST-VPS.../CGST-VPS...` reversal pair sharing the same Ref# and the tokens "VPS"/"RATE"/"REF" — get treated as duplicates of each other and one is silently dropped. Fixed by adding `vps`/`rate`/`ref` to `_GENERIC_WORDS` (structural boilerplate, not merchant-identifying). If a future statement format introduces a new generic infra token like this, add it here rather than loosening the matcher generally.

13. **`FinanceEngine.effective_monthly_income()`** (not `monthly_income()`) is the one to use for "how much did I actually make this month" — `monthly_income()` alone just sums manually-entered Income sources, with no awareness of whether that same salary has also shown up as a real transaction. `effective_monthly_income()` matches each income source's expected amount (±5%) against this month's real transactions and only adds the *unmatched* remainder, so manually-entered income and statement-detected income never double-count. `balance_estimate()`, `overview()["monthly_income"]`, and `budget_suggest.py` all use it; `monthly_income()` itself is only correct for "what did I say my income sources are" displays (e.g. the raw Income-tab total).
