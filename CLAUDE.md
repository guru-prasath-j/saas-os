# CLAUDE.md — Amy PersonalOS

Fast-load context. Read before touching code.

## Run

```bash
python -m uvicorn amy.saas.app:app --host 0.0.0.0 --port 8849
# Kill: Get-NetTCPConnection -LocalPort 8849 | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

App startup auto-launches+supervises the local MCP servers behind the
Job Search/HackerNews/YouTube/Dev.to connectors (`mcp_servers/*.py`, ports
8935/8001/8003/8004 — see `_local_mcp_supervisor_loop` in `amy/saas/app.py`).
Force-killing the main app (above) does **not** kill these children on
Windows — they keep running independently, which is intentional (the
supervisor detects they're already up on next start and won't duplicate
them). To stop everything including the children:
```bash
# Kill main app + all four local MCP servers:
Get-NetTCPConnection -LocalPort 8849,8935,8001,8003,8004 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```
Opt out of auto-start with `AMY_LOCAL_MCP_SERVERS=0`. YouTube needs
`YOUTUBE_API_KEY` in `.env` (YouTube Data API v3) — without it the server
still starts but `search_videos` returns `[]`.

## What It Is

FastAPI + single-page frontend. SQLite-per-user, multi-user capable — do
not hardcode a uid/email in docs or scripts, it goes stale fast; look up
current accounts in `saas_data/amy_saas.db`'s `users` table
(`id, email`).

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
  automation/
    store.py             AutomationStore: jobs/runs/approvals/llm_calls tables (collab.db) + TrackedLLM
    executors.py         JobCtx + tier router (submit_action) + agent_gate + approval executors
    jobs.py              Handler registry, DEFAULT_JOBS, run_due (called by app loop)
    ingest.py            Hybrid Gmail statement-attachment auto-ingest + LLM column-map proposal
    learning.py          Learned categorizer rules (corrections → rules, finance.db)
    sentinels.py         Anomaly sentinel (dupes/large debits/price hikes/run-rate) + goal drift
    closers.py           Monthly close, custodial autopilot, morning briefing (R5), daily Autopilot
    assistant.py         AI chat console: JSON tool loop over the tool registry
    orchestrator.py      /api/agent/goal: plan → gated tools → GraphStore plan graph
    audit.py             build_audit_report: regulator-style joined report
  tools/                 Tool registry (R1): JSON-schema tools with risk levels
                         read|write|destructive; AGENT_GATE parks agent writes
  agents/reactive.py     Reactive agents (R2): budget/subscription/compliance/
                         screening/errand/learning — wired onto EventStore at
                         emit points (register_reactive_agents(events, ctx))
  calendars/             Calendar abstraction (R7A-3): gregorian|hijri|fiscal
  jurisdictions/         Packs (R7B): {uae,us,india}.json + loader/versioning
                         + fx_seed.json. New jurisdiction = new JSON only
  obligations/           Obligations engine + agent (R7A-2): zakat/advance tax/
                         quarterly estimates/savings as pack presets
    zakat.py             Full zakat: live gold/silver nisab (gold-api.com,
                         daily-cached, FX-converted), hawl from balance history
                         on the Hijri calendar, wealth breakdown (custodial
                         hard-excluded). GET /api/obligations/zakat +
                         POST .../zakat/propose (parks payment in Approval
                         Inbox); "zakat/nisab/hawl" in /api/ask intercepts
                         (vault.py:_try_zakat_answer, local-only LLM);
                         zakat_status registry tool for agents.
                         Purification agent: incoming interest flagged by
                         values screening → proposes donating the exact amount
                         (reactive.py, dedup purify_{txn_id}). Audit report
                         metadata.governance = AI-governance summary
                         (oversight counts, tools-by-risk, data locality) —
                         "Regulator report" download button on the Agent tab.
  values/                Values screening (R7A-1): presets.json + profiles +
                         screening_flags (collab.db, joined by audit)
  geo/                   Context layer (docs/CONTEXT_PLAN.md C1-C2): GeoStore
                         (geo_places/visits/cells in collab.db, enter/leave
                         hysteresis, ~110m LOCAL-day cells for unmatched fixes)
                         + learn.py merchant×cell correlator → tier-2 add_place
                         proposals. Router saas/routers/geo.py: /api/context/
                         location|status|visits|places + task place-tag.
                         Errand + spend_caution agents in reactive.py react to
                         context.place_entered; coordinates never reach an LLM.
  commitments/           Deadline-bearing life admin (CONTEXT_PLAN C3):
                         commitments table in finance.db; return-window +
                         warranty auto-detection from transactions (heuristic,
                         no LLM); commitment_scan job walks the 3d/14d ladder
                         + auto-expires. Routes: /api/commitments CRUD
                         (saas/routers/commitments.py).
  patterns.py            Behavior cadences (C4/C5): generic cadence() +
                         merchant_cadences → pattern_tasks job (prefilled
                         add_task proposals, place_tag armed for the errand
                         agent) + person_cadences → relationship_nudges job
                         (advisory, 3-day window, never a nag).
  automation/drift.py    Preference drift (C7): monthly signals from decided
                         approvals — always_reject / always_approve / ignored.
                         saas/routers/inbox.py = universal inbox (C6):
                         /api/inbox/propose|pending|decisions lets external
                         systems (whatsapp_brain…) park tier-2 drafts and act
                         only on human-approved rows (external_draft executor
                         is an ack-only no-op).
  financing.py           Financing models (R7A-4): amortized|markup|zero|lease
  fx.py                  FxConverter (pluggable source, daily cache) + multi_currency_summary
  locale_fmt.py          lakh/crore vs western grouping, format_money, prompt_hint
  events/
    store.py             EventStore: persist to collab.db events table + pub/sub
                         (failing subscribers retried once → event_dead_letters)
    triggers.py          Default subscribers (goal, vault, all finance events)
  captures.py            Photo ingestion (08_Captures: image + caption/OCR/place note)
                         + photo-memory search: search_captures/captures_between/
                         context_block — used by CollabMaster chat context, the
                         search_captures/recent_captures tools, and capture_digest
  learning_feed/          Multi-focus learning tracker (Learn tab + dashboard
                         card). aggregator.py fans a topic out to promoted MCP
                         sources (SOURCE_TOOLS: HN/YouTube/arXiv/Reddit/
                         Bluesky/Dev.to) → ranker.py (one LLM call, 0-10
                         relevance + why) → sensor.py (poll_one per
                         learning_focuses row, poll_all loops every active
                         focus; upserts learning_feed_items, emits
                         learning.feed_refreshed, writes a vault note).
                         Local MCP servers for HN/YouTube/Dev.to live in
                         mcp_servers/*.py (see Run section). Reactive agent:
                         agents/reactive.py:_learning_agent. Full detail:
                         "Learning Feed" section below.
  memory/writer.py       MemoryWriter: idempotent vault journaling (daily + atomic notes)
  knowledge_graph/store.py  GraphStore: typed nodes+edges, edge UPSERT with timestamps
  saas/
    app.py               FastAPI entry — all routers included
    db.py                SQLAlchemy users table (amy_saas.db)
    deps.py              current_user, _user_key(), _connector_dir()
    paths.py             saas_data/index/{uid}/
    routers/
      finance.py         ~60 finance endpoints (main active router, ~1300 lines)
      automation.py      Jobs/runs/Approval Inbox/llm-stats/dead-letters + /api/assistant/chat
      agent.py           /api/agent/goal + /api/agent/goals + /api/agent/audit
      jurisdictions.py   Packs, deadlines, /api/settings/locale, /api/finance/overview/fx
      obligations.py     /api/obligations activate/status/config
      values.py          /api/values presets/profiles/flags
      auth.py            JWT login, OpenAI key, private folder
      connectors.py      Google OAuth flow + disconnect
      learning_feed.py   Focus CRUD, feed list, save/progress — /api/learning-feed/...
      [10 others: vault, knowledge, habits, events, memory, twin, intelligence, agents…]
    static/index.html    Entire frontend (~3000 lines, one file)
```

## Data Paths

`{uid}` = a row id from `saas_data/amy_saas.db`'s `users` table — look it
up, don't hardcode one (see "What It Is").

| What | Path |
|---|---|
| User DB | `saas_data/amy_saas.db` |
| Finance DB | `saas_data/index/{uid}/finance.db` |
| Collab DB | `saas_data/index/{uid}/collab.db` — events, automation_jobs/runs/approvals, goals/tasks/milestones, learning_focuses/learning_feed_items, geo_places/visits/cells, values screening_flags, prefs, activities |
| Google token | `saas_data/index/{uid}/connectors/google_token.json` |

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

## Learning Feed

Multi-focus learning tracker behind the Learn tab + dashboard Learning card.

- Data lives in `collab.db` (tables created lazily by `AutomationStore._init`):
  `learning_focuses` (id, uid, topic, goal_id nullable FK into `goals`,
  active, created_at) and `learning_feed_items` (id, uid, source, title,
  url, summary, score, relevance, why, focus_tag, focus_id FK, saved,
  fetched_at, published_at, progress, position_sec, duration_sec,
  completed_at).
- A user tracks any number of topics ("focuses") at once; a focus can
  optionally link to a `goals` row — same `goals`/`tasks` tables
  `GoalEngine` (`amy/autonomous/goals.py`, used by the `create_goal`/
  `add_goal_task` tools) and `PlannerAgent` (`amy/collab/planner.py`,
  backs `/api/goals`) both operate on, so a linked goal shows up on the
  normal Goals tab too — no separate goal model for learning.
- Pipeline: `amy/learning_feed/aggregator.py` (fan a topic out to every
  promoted MCP connector matching `SOURCE_TOOLS`, normalize whatever
  shape comes back — JSON, wrapped list, or numbered plain text) →
  `ranker.py` (ONE LLM call scores every item 0-10 + a one-line "why",
  degrades to unranked input order on any parse/LLM failure — never
  errors) → `sensor.py` (`LearningFeedSensor.poll_one` handles one focus
  row: fetch → rank → upsert → emit `learning.feed_refreshed` → vault
  note; `poll_all` loops every active focus for a user, one failing
  focus never blocks the others). `learning_feed_refresh` automation job
  (every 6h, gated by `AMY_LEARNING_FEED_ENABLED`) calls `poll_all()`.
- Local MCP servers for HackerNews/YouTube/Dev.to: `mcp_servers/*.py`
  (ports/env in the Run section above). arXiv/Reddit/Bluesky need an
  external community MCP server registered instead — `SOURCE_TOOLS` in
  `aggregator.py` lists the candidate tool names per source.
- Saving an item (`POST .../save/{id}`) or crossing 90% watched
  (`PATCH .../progress/{id}`) both write a vault note (`MemoryWriter`)
  AND log to the `activities` table (`MemoryManager.log_activity`,
  `amy/collab/memory.py`) — this feeds `amy/collab/learning.py`'s trend
  engine (`/api/learn`, the "Learning trends" card), so trends reflect
  real feed engagement now, not only CollabMaster chat queries.
- Reactive agent `agents/reactive.py:_learning_agent` (kill switch
  `AMY_AGENT_LEARNING`, default on) subscribes to `learning.feed_refreshed`
  and `learning.item_completed`: proposes a goal (tier-2 Approval Inbox
  via `tools.invoke(..., actor="agent")`, dedup key
  `learning_goal_{topic}`) when an UNLINKED focus's topic is trending in
  the activity log; nudges (advisory `agent.insight` only — never a
  write) a GOAL-LINKED focus that's accumulated ≥10 fetched items with
  zero saves/completions after 3+ days; journals completions as insights.
- Focus create/reactivate schedules a `BackgroundTasks` refresh keyed on
  the focus **id** (`refresh_for_user(..., focus_id=...)`), not topic
  text — refreshing by text alone would silently recreate a focus the
  user deletes in the few seconds before the queued task runs (it looks
  up-or-creates by topic string). `refresh_for_user(..., focus=...)`
  (text-keyed) is a legacy fallback kept for callers with no row id.

```
GET/POST                /api/learning-feed/focuses
PATCH/DELETE            /api/learning-feed/focuses/{focus_id}
GET                     /api/learning-feed?source=&saved=&focus_id=&limit=
POST                    /api/learning-feed/save/{item_id}
PATCH                   /api/learning-feed/progress/{item_id}   # watch-progress heartbeat, ≥90% = completed
```

## Automation Layer

App loop ticks every 60s (`AMY_AUTOMATION_TICK_SECONDS`), runs due jobs per user,
logs every run to `automation_runs`. All automated writes go through
`submit_action(ctx, tier, …)` — **tier 0** auto, **tier 1** auto+notify,
**tier 2** parked in the Approval Inbox until approved. Executors:
`import_statement` · `custodial_disburse` · `add_subscription` · `set_budget` ·
`add_transaction` · `add_place` · `add_task` · `external_draft` (ack-only).
Approve/reject decisions are recorded via DecisionEngine.

Default jobs: `gmail_statement_ingest` (6h, hybrid: saved-map/preset/pdfplumber
→ auto-import tier 1; auto-detect/LLM-map/ambiguous → tier 2 approval, map saved
on approve) · `auto_categorize` (12h, learned rules first) · `anomaly_sentinel` ·
`cashflow_alerts` · `morning_briefing` (07:00, email if SMTP set) ·
`custodial_autopilot` (proposes prefilled cycle as tier 2) · `autopilot` (05:00) ·
`monthly_close` (1st, CFO report + subscription proposals + compliance refresh) ·
`capture_digest` (20:30, photo-memory day-over-day compare, Sunday = weekly
rollup, writes 09_Memory note so chat recalls it next day) · `place_learning`
(21:00, geo_cells×merchant correlation → tier-2 add_place proposals) ·
`commitment_scan` (08:20, return-window/warranty detection + deadline ladder) ·
`pattern_tasks` (06:30, cadence-due merchants → prefilled task proposals) ·
`relationship_nudges` (09:00, broken transfer rhythms → advisory nudge) ·
`preference_drift` (monthly 2nd, decision-history signals).

```
GET               /api/automation/status | jobs | runs | llm-stats | dead-letters | learned-rules
PATCH             /api/automation/jobs/{name}            # enable/disable/schedule
POST              /api/automation/jobs/{name}/run
GET               /api/automation/approvals?status=pending|all
POST              /api/automation/approvals/{aid}/approve | reject
POST              /api/automation/pause | resume          # global kill switch
POST              /api/assistant/chat                     # {message, history} → JSON tool loop
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
learning.feed_refreshed / learning.item_completed
agent.insight / agent.action_proposed / agent.action_executed
agent.goal_planned / agent.error        # always carry {agent, reasoning}
vault.note_edited
goal.created / goal.completed / capture.added / digest.generated
context.place_entered / place_left / location_updated   # payload = place id/name/kind, never coordinates

# Reactive agents (amy/agents/reactive.py) are wired onto EventStore at
# EVERY site that builds one and is about to emit an agent-relevant event —
# the bus is per-instance, so each call site must call
# register_reactive_agents(events, ctx) itself. Known sites: _emit_fin
# (finance.py), JobCtx.events() (automation/executors.py),
# _events_with_agents (geo.py), and the learning-feed router/sensor. A site
# that builds a bare EventStore(cdb) and calls .emit() directly SILENTLY
# drops all agent reactions — no error, nothing in the logs. Kill switches:
# AMY_AGENT_BUDGET / _SUBSCRIPTION / _COMPLIANCE / _SCREENING / _OBLIGATION /
# _ERRAND / _LEARNING.
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
12. Automation tables (jobs/runs/approvals/llm_calls) are created lazily by `AutomationStore.__init__` in collab.db; `learned_category_rules` lives in finance.db (created by `amy/automation/learning.py`).
13. PATCH `/transactions/{tid}/category` also saves a learned rule — the categorizer converges from corrections; check `learned_category_rules` before assuming a static rule matched.
14. The assistant (`/api/assistant/chat`) expects ONE JSON object per LLM turn; `_parse_step` takes the first complete object (models sometimes emit several tool calls at once) — but it filters for tool/final keys; the orchestrator has its own `_first_obj` without that filter (plans/summaries are arbitrary objects).
15. Any registry tool invoked with `actor="agent"` and risk write/destructive parks in the Approval Inbox via `AGENT_GATE` (installed at `import amy.automation`). Agents set `ctx._extras["agent_name"/"agent_reasoning"/"agent_dedup_key"]` BEFORE `tools.invoke`. Destructive tier is hard-pinned to 2; `AMY_AGENT_WRITE_TIER` only affects writes.
16. JWT secret: `AMY_JWT_SECRET` env (≥32 chars) or auto-generated at `saas_data/.jwt_secret`. `AMY_ENC_SECRET` fallback stays the legacy constant on purpose (stored keys stay decryptable). `DELETE /api/finance/transactions` needs `?confirm=DELETE-ALL-TRANSACTIONS`.
17. Jurisdiction packs: everything country-specific is JSON in `amy/jurisdictions/` (validated on load, effective-date versioned). No jurisdiction/religion logic in Python — new jurisdiction = new pack file (docs/jurisdictions.md). Obligation/screening presets are data; custodial accounts are excluded from obligation wealth math as a hard rail packs cannot override.
18. Currency display: never hardcode ₹ — backend formats via `amy/locale_fmt.py` (+ `AMY_CURRENCY_SYMBOL` for context.py), frontend via `fmtMoney()` driven by `/api/settings/locale`.
19. `docs/AGENT_PLAN.md` is the source of truth for the agentic-finance project (phases, commits, binding R7B spec).
20. A new event-emit site ALWAYS needs its own `register_reactive_agents(events, ctx)` call before `.emit(...)` — copy the `_emit_fin`/`_events_with_agents` idiom. Forgetting it doesn't error; the agent just never fires (found and fixed twice in the learning-feed router/sensor during that feature's build).
21. Refresh-by-topic-text on a multi-row feature (learning_focuses) can resurrect a row a user just deleted, if a `BackgroundTasks` refresh was queued before the delete lands and only runs after. Refresh by row id, not by the text a lookup-or-create query matches on, whenever an id is available.

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
