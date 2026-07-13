# PROJECT_CONTEXT.md — Amy PersonalOS, full project brain

**Purpose of this file:** a single, self-contained reference you can paste into a
fresh Claude conversation to discuss what to build next, without Claude having
to re-derive the codebase from scratch. It covers what the product is, every
data table, every API route, every event/job/agent, what's known to be
missing or stubbed, and a menu of concrete next-feature ideas. It duplicates
some of `CLAUDE.md` (the day-to-day coding reference) on purpose — this file
is meant to travel alone.

Generated from the codebase at commit `1040dfb` (2026-07-13) plus 5
uncommitted working-tree changes (noted inline where relevant). Treat
specifics (line numbers, exact column lists) as accurate as of that point —
re-verify against the repo before relying on them for a change, the same way
you would treat any snapshot.

---

## 1. What this is

**Amy — PersonalOS**: a self-hosted personal AI operating system. FastAPI
backend + one single-page HTML/JS frontend (`amy/saas/static/index.html`,
~5000 lines, no build step, no framework). SQLite per user (not one shared
DB) — multi-tenant in principle, but built and run for one primary user.
Auth is JWT bearer.

The flagship module is **Finance CFO**: import bank statements (CSV/XLS/PDF),
sync Gmail for transaction emails, auto-categorize, budgets, subscriptions,
investments, income sources, custodial (pass-through) accounts, side-business
bookkeeping, tax/zakat obligations. Layered on top of that core is a growing
set of "Autopilot" systems that turn passive data into proactive action:
**Career Autopilot** (job discovery → application → follow-up), **Life
Autopilot** (health targets, habit auto-tracking, wellbeing index, place-
triggered nudges), a **Learning Feed** (multi-topic content tracker), and
**Connectors** (GitHub/Plane via MCP). All of it sits on a shared spine: an
**event bus**, a **tool registry with tiered agent-write gating** (auto /
auto+notify / human-approval), a **vault** (Obsidian-style markdown journal
auto-written from events), and a **knowledge graph**.

Design philosophy enforced throughout (see §13 for where it's tested):
advisory over diagnostic, propose-don't-impose (new habits/goals/spend are
always human-approved), honest `None`/`available:false` over LLM-fabricated
data, real data sources only (no simulated job postings, no invented company
intel), sensitive data routed to a local-only LLM, never a nag (dedup +
drift-pruning on every proactive suggestion).

## 2. Run / environment

```bash
pip install -r requirements.txt
cp .env.example .env          # NVIDIA_API_KEY, GOOGLE_CLIENT_ID/SECRET, etc.
python -m uvicorn amy.saas.app:app --host 0.0.0.0 --port 8849
```

App startup auto-launches 4 local MCP servers (Job Search/HackerNews/
YouTube/Dev.to/Courses, ports 8935/8001/8003/8004/8005) via
`_local_mcp_supervisor_loop` in `amy/saas/app.py`; opt out with
`AMY_LOCAL_MCP_SERVERS=0`. Force-killing the main app on Windows does NOT
kill these children — see `CLAUDE.md`'s Run section for the full kill
command across all 5 ports.

Stack: Python 3 / FastAPI / SQLAlchemy (only for the shared `amy_saas.db`) /
raw `sqlite3` (everything else) / vanilla JS frontend / Jinja-free static
HTML. LLM providers: NVIDIA → OpenAI → Groq → local Ollama → rule-based
template fallback (`amy/llm.py::LLMRouter`), selectable per-call as
`sensitive=True` to force local-only routing.

## 3. Full module tree

```
amy/
  config.py, llm.py, context.py, vault_watcher.py, locale_fmt.py, fx.py, financing.py, patterns.py

  finance/
    engine.py            FinanceEngine — SQLite wrapper, ~60 methods
    categorizer.py        rule-based instant categorizer
    afford.py             "Can I afford this?"
    budget_suggest.py     LLM budget caps
    subscription_detect.py / investment_detect.py / income_detect.py   recurrence detectors
    custodial.py / custodial_sheets.py / custodial_ai.py
    business/              entities, accountant, auditor, compliance, sensitivity, rates
    sync/                  csv_import, pdf_import, gmail_import, bank_presets, gmail_sensor
    dedup.py

  automation/
    store.py               AutomationStore — jobs/runs/approvals/llm_calls + all the newer tables (collab.db)
    executors.py            JobCtx, tier router, agent_gate, approval executors
    jobs.py                 handler registry, DEFAULT_JOBS, run_due()
    ingest.py                Gmail statement auto-ingest + LLM column-map proposal
    learning.py               learned categorizer rules (finance.db)
    sentinels.py               anomaly sentinel + goal drift
    closers.py                  monthly close, custodial autopilot, morning briefing, life section
    assistant.py                 /api/assistant/chat JSON tool loop
    orchestrator.py               /api/agent/goal plan→gate→graph, career-goal templated fan-out
    audit.py                      regulator-style joined audit report
    drift.py                        preference drift (always_reject/approve/ignored)

  tools/                  JSON-schema tool registry (risk: read|write|destructive) + AGENT_GATE
  agents/reactive.py      all event-subscribed reactive agents (see §10)
  agents/folders.py       persona sub-agents backing /api/ask (AMY_DYNAMIC_AGENTS off = default)
  agents/master.py        MasterAgent for /api/ask (amy/engine.py::Engine)
  agents/career.py        legacy CareerAgent wired into CollabMaster (/api/collab/ask)

  calendars/              gregorian|hijri|fiscal abstraction
  jurisdictions/          country packs (JSON: uae/us/india) + fx_seed.json
  obligations/            zakat.py (full live nisab/hawl), engine + agent
  values/                 screening presets/profiles/flags
  geo/                    GeoStore (places/visits/cells), learn.py cell×merchant correlator
  commitments/            deadline ladder (return windows, warranties, custom)

  patterns.py             cadence() + merchant/person cadence detection

  career_scout.py         JobScoutSensor — job discovery + match scoring
  career_apply.py         application pipeline (channel/ATS/intel/draft/approval/follow-up)
  career_inbound.py       inbound HR-reply detection riding the Gmail poll

  life/                   Life Autopilot: targets.py, bootstrap.py, aggregator.py, baselines.py,
                           inference.py, habits.py, opportunity_rules.py/opportunity.py,
                           wellbeing.py, review.py, meal_captures.py, commitments_life.py, health_data.py

  learning_feed/          aggregator.py (fan-out), ranker.py (LLM score), sensor.py (poll)

  memory/writer.py        MemoryWriter — idempotent vault journaling
  knowledge_graph/store.py  GraphStore — cross-source typed nodes+edges
  knowledge/              vault RAG: chunking, embeddings, retrieval, search, metadata, relationships, confidence
  captures.py             photo ingestion + photo-memory search

  events/
    store.py               EventStore (collab.db `events` table) + pub/sub + dead-letter retry
    factory.py               get_events() — the ONE way to build a store whose emits reach reactive agents
    triggers.py               default subscribers (goal/vault/finance events)

  connectors/
    mcp.py                  generic MCP client (list_tools/call_tool)
    mcp_call.py               call_mcp_tool() — resolve connector → pick candidate tool → call → log
    sensors.py                  GitHubSensor / PlaneSensor

  sensors/                 legacy Operational-Layer GitHub sensor (github_sensor.py/service.py/models.py) —
                            still live via amy/sensors/mcp_sensor.py, NOT the same as connectors/sensors.py
  operational/sensors.py   Sensor / SensorRegistry base classes (still load-bearing, everything subclasses this)

  saas/
    app.py                 FastAPI entry — all routers, local-MCP supervisor, automation tick loop
    db.py                  SQLAlchemy models: User, ImportJob, McpConnector (amy_saas.db)
    deps.py                 current_user, _user_key, _engine_for, _collab_db_path, _knowledge_for, _connector_dir
    paths.py                 SAAS_DATA root; vault_dir/index_dir/uploads_dir per uid
    security.py               Fernet encryption for stored secrets (API keys, resume text, health constraints)
    tenancy.py                 per-user vault directory resolution
    routers/                (~30 files — full catalog in §7)
    static/index.html        entire frontend
```

## 4. Data storage & multi-tenancy

Nothing is hardcoded to one user — every path is keyed by `{uid}`, a row id
from `amy_saas.db`'s `users` table. `SAAS_DATA` root defaults to
`<repo>/saas_data` (override via `AMY_SAAS_DATA` env var), gitignored.

| Path | Contents |
|---|---|
| `saas_data/amy_saas.db` | shared SQLAlchemy DB: `users`, `import_jobs`, `mcp_connectors` |
| `saas_data/vaults/{uid}/` | per-user Obsidian-style markdown vault (00_Daily, 09_Memory, etc.) |
| `saas_data/index/{uid}/finance.db` | all finance + business + obligations + values-profile tables |
| `saas_data/index/{uid}/collab.db` | events, automation, goals, career, life, geo, commitments, notifications |
| `saas_data/index/{uid}/habits.db` | habits + habit_logs (`HabitEngine`) |
| `saas_data/index/{uid}/srs.db` | spaced-repetition cards (`SRSEngine`) |
| `saas_data/index/{uid}/entities.db` | extracted vault entities (`EntityExtractor`) |
| `saas_data/index/{uid}/graph.db` | knowledge-graph nodes/edges (`GraphStore`) |
| `saas_data/index/{uid}/knowledge/` | RAG store — separate `metadata`/`vector`/`agents` sqlite files (`KnowledgeBase`) |
| `saas_data/index/{uid}/connectors/google_token.json` | Google OAuth token |
| `saas_data/uploads/{uid}/` | uploaded files pending processing |

Every per-user DB is created lazily by its own engine's `__init__`
(`CREATE TABLE IF NOT EXISTS`) — there is no central migration runner;
schema changes ship as idempotent `ALTER TABLE` blocks in each engine's
`_migrate()` (see quirk 16 in `CLAUDE.md`: no Alembic yet, a known gap —
see §13).

## 5. Database schema reference

### 5.1 `amy_saas.db` (shared, SQLAlchemy)

**`users`** — `id, email, password_hash, openai_key_enc, sensitive_folders,
aa_enabled, location, home_jurisdiction (default 'india'),
active_jurisdictions, language, created_at`

**`import_jobs`** — `id, user_id, status(pending|running|done|failed),
markdown_notes, notes_loaded, error, created_at, finished_at`

**`mcp_connectors`** — `id, user_id, name, server_url, auth_type
(none|api_key|oauth), auth_ref (Fernet-encrypted), risk_tier
(official|platform_api|scraping_backed|unofficial_risky),
promoted_to_sensor, created_at, auth_extra (plaintext, e.g. a workspace
slug), default_target (server-side target, e.g. "owner/repo")`

### 5.2 `finance.db` (per user)

**`transactions`** — `id, date, amount, category, merchant, source, notes` +
migrated: `account_id, beneficiary_id, screenshot_path, part, currency`.
Indexes on `date`, `category`, `account_id`, `beneficiary_id`.

**`budgets`** — `category (PK), monthly_limit`

**`subscriptions`** — `id, name, monthly_cost, annual_cost, renewal_date,
auto_renew, payment_method, status`

**`investments`** — `id, type, name, current_value, cost_basis`
(`current_value` defaults to `cost_basis` — no live price feed)

**`income_sources`** — `id, name, type, amount, recurrence`

**`accounts`** — `id, nickname, bank_name, account_type
(savings|current|credit_card|investment|custodial), sync_method,
last_synced_at, created_at, meta` + migrated: `jurisdiction, currency`

**`bank_column_maps`** — `bank_name (PK), column_map`

**`beneficiaries`** — `id, account_id, name, split_kind, default_parts,
sheet_tab, active, created_at` + migrated: `tracking_only, expected_amount`

**`business_entities`** — `id, name, pan, gstin, constitution
(proprietorship default), registration_state, financial_year, tax_regime,
holds_depreciable_assets, tracking_closeness (loose default), created_at`
+ migrated: `jurisdiction`

**`ledger_entries`** — `id, business_entity_id, date, amount, description,
category, source_event_id, source_document, confidence, posted_by
(default 'accountant'), audit_status (default 'unaudited'), created_at`

**`compliance_suggestions`** — `id, business_entity_id, ledger_entry_id,
source_event_id, suggestion_type, reasoning, rate_used, citation,
ca_disclaimer, routed_sensitive, created_at`

**`rate_table`** — `id, rate_type, key, value, effective_from,
effective_to, source_note, updated_at`

**`suggestion_cache`** — `kind (PK), payload, computed_at`

**`values_profiles`** — `id, preset_id, name, rules, enabled, created_at`
(lives in finance.db, not collab.db)

**`user_obligations`** — `id, jurisdiction, preset_id, status, config,
activated_at`, unique `(jurisdiction, preset_id)`

**`learned_category_rules`** — `id, pattern (unique), category,
created_at, hits` (also finance.db — see `CLAUDE.md` quirk 12)

### 5.3 `collab.db` (per user — events, automation, goals, career, life…)

From `amy/collab/db.py`:
- **`prefs`** — `key (PK), value`
- **`summaries`** — `id, ts, text`
- **`activities`** — `id, ts, kind, detail, domain` (feeds the learning-trend engine)
- **`note_access`** — `path (PK), count, last_ts`
- **`agent_cards`** — `agent (PK), topics, faqs, last_files, importance, updated_at`
- **`goals`** — `id, title, domain, status, progress, created_at, target_date` + migrated `finance_meta`, `career_meta` (JSON)
- **`milestones`** — `id, goal_id, title, done, position`
- **`tasks`** — `id, goal_id, title, done, created_at` + migrated `place_tag`
- **`goal_deps`** — `goal_id, depends_on`, unique pair
- **`decisions`** — `id, ts, title, reason, domain, confidence, outcome, status`
- **`agent_state`** — `agent (PK), enabled`
- **`events`** — `id, ts, type, payload, source` (the event log)
- **`op_entities` / `op_connector_state`** — legacy Operational Layer tables, `CREATE TABLE IF NOT EXISTS` kept but unused (see §13)
- **`notifications`** — `id, type, title, body, created_at, read_at, priority, related_entity`

From `amy/automation/store.py` (`AutomationStore`):
- **`automation_jobs`** — `name (PK), schedule, enabled, last_run_at, next_run_at, last_status, config`
- **`automation_runs`** — `id, job_name, started_at, finished_at, status, detail`
- **`approvals`** — `id, created_at, decided_at, tier, action_type, title, body, payload, status, result, source, dedup_key` + migrated `reasoning, risk, affected_entity, expires_at`
- **`llm_calls`** — `id, ts, provider, purpose, ok, ms, error`
- **`learning_feed_items`** — `id, uid, source, title, url, summary, score, relevance, why, focus_tag, saved, fetched_at, published_at` + migrated `progress, position_sec, duration_sec, completed_at, focus_id`
- **`ingested_attachments`** — `(msg_id, filename) PK, sha256, status, ts, detail`
- **`learning_focuses`** — `id, uid, topic, goal_id, active, created_at`
- **`connector_sensor_seen`** — `(sensor, item_key) PK, state, ts`
- **`connector_calls`** — `id, uid, connector, tool, ok, ms, error, ts`
- **`career_profile`** — `uid (PK), target_role, target_location, remote_ok, deadline, resume_text_enc, skills, updated_at`
- **`job_postings`** — `id, uid, source, title, company, url, location, salary, is_remote, description, keywords, match_score, match_factors, status (discovered default), discovered_at` + migrated `alt_sources`; unique `(uid, url)`
- **`applications`** — `id, uid, posting_id, channel, status (prepared→approved→sent→response→interview→offer, or rejected/ghosted), match_score, ats_estimate, draft, timeline (JSON), created_at, updated_at` + migrated `thread_refs`
- **`company_intel`** — `(uid, company) PK, notes, sources, cached_at`
- **`life_metrics`** — `uid, date, office_minutes, commute_out/return_minutes, left_office_at, gym_visits, home_arrival_at, sleep_window_start/end, sleep_estimate_min, meals_out, late_night_orders, cafe_spend, meeting_count, …` (day-type/grace columns follow) + migrated `sleep_provenance`
- **`habit_links`** — `id, uid, habit_id, signal_type, signal_params, mode (auto_complete|auto_suggest_check), created_at`
- **`wellbeing_weekly`** — `(uid, week) PK, components (JSON), index_delta, line_emitted, computed_at`
- **`health_profile`** — `uid (PK), dob_or_age, sex, height_cm, weight_kg, activity_level, weight_log (JSON), constraints_enc, provenance (JSON), updated_at` + migrated `targets (JSON)`

From `amy/events/store.py`: **`event_dead_letters`** — `id, ts, event_id, event_type, handler, error, retries`

From `amy/knowledge_graph/store.py` — actually its own file, `graph.db` (see §4), not collab.db: **`nodes`** (`id, type, label, ref`), **`edges`** (`id, src, dst, rel, weight, created_at, updated_at`, unique `(src,dst,rel)`)

From `amy/geo/store.py`: **`geo_places`** (`id, name, kind, lat, lon, radius_m, source, meta, created_at`), **`geo_visits`** (`id, place_id, entered_at, left_at`), **`geo_state`** (`key PK, value`), **`geo_cells`** (`(cell, day) PK, hits`)

From `amy/commitments/engine.py`: **`commitments`** — `id, kind, title, merchant, amount, ref_txn_id, source, start_date, due_date, status (open default), notes, meta, created_at`

From `amy/values/__init__.py`: **`screening_flags`** — `id, created_at, transaction_id, profile_id, profile_name, rule_kind, severity, reasoning, status (open default)`; **`screened_txns`** — `transaction_id (PK), ts` (both on the collab connection)

From `amy/automation/orchestrator.py`: **`agent_goals`** — `id, ts, goal, plan, steps, summary, status`

### 5.4 Other per-user DBs

- **`habits.db`**: `habits` (`id, title, frequency, color, created_at, archived`), `habit_logs` (`id, habit_id, date, done, note`, unique `(habit_id,date)`)
- **`srs.db`**: `srs_cards` (`id, note_path, front, back, interval, ease, due_date, reviews, created_at`)
- **`entities.db`**: `entities` (`id, name, type, mentions, note_paths, last_seen`)
- **`graph.db`**: see 5.3 nodes/edges
- **`knowledge/metadata.db`**: `notes` (path/title/summary/domain/subdomains/entities/keywords/tags/importance/embedding_id), `relationships` (`src_id, dst_id, rel_type, weight`, unique triple)
- **`knowledge/vector.db`**: `chunks` (`id, note_id, chunk_index, text, embedding (JSON), dim, model`)
- **`knowledge/agents.db`**: `agents` (`name (PK), domain, note_count, config`)

## 6. Full API endpoint catalog

All routes require `Authorization: Bearer <token>` except `/auth/signup`,
`/auth/login`, and the Google OAuth callback. ~30 router files, ~250 routes
total. Grouped by router file (`amy/saas/routers/*.py`).

### auth.py — signup/login, account settings
`POST /auth/signup` · `POST /auth/login` · `GET /api/me` ·
`POST/DELETE /api/settings/openai-key` (BYO key, encrypted) ·
`GET/PUT /api/settings/private-folders` (vault prefixes forced to local-only LLM) ·
`GET/POST /api/settings/vault` (vault path/location) ·
`GET/POST /api/settings/aa-enabled` (Account Aggregator kill switch) ·
`POST /api/settings/location`

### finance.py — the core CFO module (~85 routes)
Full detail already lives in `CLAUDE.md`'s "Finance API Routes" section —
reproduced compactly here. Groups: Transactions (CRUD, auto-categorize,
duplicates), Overview/forecast (`/api/finance/overview` now returns a
`period` field — see quirk 25 below), Accounts (CRUD, CSV/PDF/investments
preview+upload, column-map, bank-presets), Gmail/AA sync, Budgets (+
suggestions), Subscriptions/Investments/Income (each with a `/suggestions`
detector endpoint), `POST /api/finance/afford`, `GET /api/finance/goals`,
`POST /api/finance/calendar/sync`, and the full Custodial sub-tree
(beneficiaries, next-cycle-prefill, disburse, Sheet link/analyze/import,
screenshot OCR parse, suggestions/confirm, precheck).

### business.py — side-business ledger/compliance
`POST/GET/PATCH/DELETE /api/business/entities[/{id}]` ·
`POST /api/business/entities/{id}/ledger/upload` ·
`GET/PATCH/DELETE /api/business/entities/{id}/ledger[/{entry_id}]` ·
`POST /api/business/entities/{id}/ledger/audit` ·
`POST/GET /api/business/entities/{id}/compliance[/run]` ·
`GET/PATCH /api/business/rates[/{rate_id}]`

### obligations.py — zakat / tax presets
`GET /api/obligations` · `GET /api/obligations/zakat` ·
`POST /api/obligations/zakat/propose` · `GET /api/obligations/presets` ·
`POST /api/obligations/activate` ·
`PATCH/POST /api/obligations/{id}[/deactivate]`

### values.py — values screening
`GET /api/values/presets` · `GET/POST /api/values/profiles` ·
`PATCH /api/values/profiles/{id}` · `GET /api/values/flags` ·
`POST /api/values/flags/{id}/dismiss`

### jurisdictions.py — country packs & locale
`GET /api/jurisdictions` · `GET /api/jurisdictions/deadlines` ·
`GET /api/jurisdictions/{pack_id}` · `GET/POST /api/settings/locale` ·
`GET /api/finance/overview/fx`

### career.py — Career Autopilot surface
`GET/PUT /api/career/profile` · `PATCH /api/career/goal` (ladder roles) ·
`GET /api/career/postings` · `GET /api/career/applications` ·
`PATCH /api/career/applications/{id}` (human-reported outcome) ·
`GET /api/career/portfolio` (has side effects — see quirk in `CLAUDE.md`) ·
`POST /api/career/postings/{id}/apply` (409 + `?force=true` on duplicate-company)

### captures.py — photo ingestion
`POST/GET /api/captures` · `GET /api/captures/image` ·
`POST /api/career/clip` (capture → career-portfolio clip)

### life.py — Life Autopilot
`GET /api/life/metrics` · `POST /api/life/opportunities/{id}/dismiss` ·
`GET /api/life/habits/link-suggestions` ·
`POST /api/life/habits/{id}/link` · `GET /api/life/habits/{id}/links` ·
`DELETE /api/life/habit-links/{id}` · `GET /api/life/habits-overview` ·
`GET /api/life/health/targets` · `GET /api/life/wellbeing[/{week}]`

### learning_feed.py — multi-focus content tracker
`GET /api/learning-feed` · `GET/POST /api/learning-feed/focuses` ·
`PATCH/DELETE /api/learning-feed/focuses/{id}` ·
`PATCH /api/learning-feed/progress/{item_id}` (watch heartbeat, ≥90%=completed) ·
`POST /api/learning-feed/save/{id}` · `POST /api/learning-feed/capture`

### connectors.py — Google OAuth + unified connector health
`GET /api/connectors/google/status` · `GET /api/connectors/google/auth` ·
`GET /api/connectors/google/callback` · `DELETE /api/connectors/google` ·
`POST /api/connectors/google/sync` · `GET /api/connectors/status`
(health rollup across Google/local-MCP/external-MCP) ·
`GET /api/connectors[/{kind}]`

### mcp_connectors.py — generic MCP registration (Layer 1)
`POST/GET /api/mcp/connectors` · `DELETE /api/mcp/connectors/{id}` ·
`PATCH /api/mcp/connectors/{id}/promote` (Layer-2 sensor opt-in) ·
`PATCH /api/mcp/connectors/{id}/target` · `POST /api/mcp/connectors/{id}/tools` ·
`POST /api/mcp/connectors/{id}/call` · `POST /api/mcp/connectors/{id}/poll`

### geo.py — location context inlet
`POST /api/context/location` (geofence/GPS fix ingest) ·
`GET /api/context/status` · `GET /api/context/visits` ·
`POST/GET /api/context/places` · `PATCH/DELETE /api/context/places/{id}` ·
`PATCH /api/context/tasks/{id}/place-tag`

### commitments.py — deadline-bearing life admin
`POST/GET /api/commitments` · `PATCH/DELETE /api/commitments/{id}`

### automation.py — job scheduler, Approval Inbox, AI chat console
`GET /api/automation/status` · `POST /api/automation/pause|resume` ·
`GET /api/automation/jobs` · `PATCH /api/automation/jobs/{name}` ·
`POST /api/automation/jobs/{name}/run` · `GET /api/automation/runs` ·
`GET /api/automation/approvals` ·
`POST /api/automation/approvals/{id}/approve|reject` ·
`GET /api/automation/llm-stats` · `GET /api/automation/dead-letters` ·
`GET /api/automation/learned-rules` · `POST /api/assistant/chat`

### agent.py — the general-purpose goal planner
`POST /api/agent/goal` (plan → gated tools → plan graph; career goals get a
templated fan-out instead — see `CLAUDE.md`) · `GET /api/agent/goals` ·
`GET /api/agent/audit`

### inbox.py — universal external-system approval inbox
`POST /api/inbox/propose` · `GET /api/inbox/pending` ·
`GET /api/inbox/decisions` (contract for any external system, e.g.
whatsapp_brain, to park drafts and act only on human decisions)

### collab.py — CollabMaster chat, goals/milestones, reflect/learn
`POST /api/collab/ask[/stream]` (federated chat context: memory + finance +
captures + live Plane MCP) · `POST/GET /api/goals` ·
`PATCH/DELETE /api/goals/{id}` ·
`POST /api/goals/{id}/milestones[/suggest]` (AI milestone suggestions) ·
`POST /api/milestones/{id}/complete` · `PATCH/DELETE /api/milestones/{id}` ·
`POST /api/goals/{id}/finance-target` · `GET /api/finance/drift` ·
`GET /api/reflect` · `GET /api/learn` (trend engine) · `GET /api/memory` ·
`POST /api/memory/pref`

### intelligence.py — decisions/predictions/simulate/executive/timeline/search
Decisions: `POST/GET /api/decisions[/v2]`, `.../history`, `.../analysis`,
`.../recommendations`, `POST /api/decisions/{id}/outcome`.
Predictive: `GET /api/predict/goals`, `GET /api/predict/{metric}`.
Simulation: `POST /api/simulate`.
Autonomous goals/executive: `GET /api/goals/overview`,
`POST /api/goals/{id}/tasks`, `POST /api/tasks/{id}/complete`,
`POST /api/goals/{id}/depends`, `GET /api/executive`.
Autopilot: `POST /api/autopilot/run`.
Context engine: `GET /api/context`, `POST /api/context/mode`.
Timeline: `GET /api/timeline[/day|/week|/month]`.
Universal search: `POST /api/search`. Unified recall: `POST /api/recall`.

### product.py — profile/portfolio/dashboard/agent toggles/digest
`GET /api/profile` · `GET /api/portfolio` (legacy, unrelated to career
portfolio) · `GET /api/dashboard` · `GET /api/agents` ·
`POST /api/agents/{agent}/enable|disable` · `GET /api/suggestions` ·
`GET /api/cards` · `GET /api/digest[/latest]`

### memory.py — vault journal / recall / heatmap
`POST /api/memory/sync` · `GET /api/memory/daily` ·
`GET /api/memory/recall` · `POST /api/memory/consolidate` ·
`GET /api/memory/patterns` · `GET /api/memory/verify` ·
`POST /api/memory/reindex` · `POST /api/memory/log` ·
`GET /api/memory/index` · `GET /api/memory/file` · `GET /api/memory/heatmap`

### knowledge.py — RAG + global knowledge graph
`POST /api/knowledge/build|ask|search` · `GET /api/knowledge/metadata|graph` ·
`POST /api/knowledge/relationship` ·
`POST /api/kg/build` · `GET /api/kg/nodes|neighbors|traverse` ·
`GET /api/graph/viz`

### habits.py — habits + SRS + entity extraction
`GET/POST /api/habits` · `POST /api/habits/{id}/checkin` ·
`DELETE /api/habits/{id}` · `GET /api/habits/{id}/heatmap` ·
`POST /api/srs/build` · `GET /api/srs/due` · `POST /api/srs/review` ·
`GET /api/srs/stats` · `POST /api/entities/build` ·
`GET /api/entities[/search]`

### twin.py — digital twin / personality / future-self
`GET /api/twin[/full]` · `POST /api/twin[/full]/ask` ·
`GET /api/personality` · `POST /api/future-self/validate`

### vault.py — vault import, notes, tags, /api/ask intercepts
`POST /api/vault/import` · `GET /api/vault/import/{job_id}` ·
`GET /api/vault` · `GET /api/notes` · `GET /api/vault/tree` ·
`DELETE /api/vault` · `DELETE /api/account` · `POST /api/query` ·
`GET /api/stats` · `POST /api/ask` (custodial/zakat/nisab/hawl
intercepts before general routing — see `CLAUDE.md`) ·
`GET /api/vault/analyze` · `GET /api/domains` · `GET /api/tags`

### events.py — event log + legacy GitHub sensor
`GET /api/events[/stats]` · `POST /api/sensors/github/webhook|poll`
(legacy Operational-Layer GitHub integration, separate from
`connectors/sensors.py`'s MCP-based one)

### notifications.py — in-app alerts
`GET /api/notifications[/count]` · `POST /api/notifications/{id}/read` ·
`POST /api/notifications/read-all` · `GET /api/notifications/stream` (SSE)

## 7. Event catalog

Emitted via `amy.events.factory.get_events(...)` (reaches reactive agents)
or a bare `EventStore(cdb)` (no agent reaction — used only where
intentional, each site commented why). Full event-type list:

```
finance.transaction_added / csv_imported / pdf_imported / gmail_synced
finance.budget_set / subscription_added / investment_added / income_added
finance.ledger_entry_posted / ledger_audited / compliance_suggested
business.entity_created
learning.feed_refreshed / learning.item_completed
agent.insight / agent.action_proposed / agent.action_executed
agent.goal_planned / agent.error
vault.note_edited
goal.created / goal.completed / capture.added / digest.generated
context.place_entered / place_left / location_updated  (place id/kind only, never coordinates)
github.pr_review_requested / pr_status_changed / issue_assigned
plane.task_assigned / task_due_soon / task_status_changed
career.goal_set / job_discovered / application_prepared / application_sent /
  application_status_changed / portfolio_analyzed
life.metrics_computed / life.pattern_detected
```

Registration is idempotent per `EventStore` instance
(`events._registered_agent_keys`); building the store via the factory twice
does not double-fire agents. A bare store emitting an `AGENT_RELEVANT_EVENTS`
type logs one WARNING per process per call-site instead of silently
dropping the reaction.

## 8. Automation jobs (scheduled, `AMY_AUTOMATION_TICK_SECONDS`, default 60s)

| Job | Cadence | Does |
|---|---|---|
| `gmail_statement_ingest` | 6h | hybrid statement-attachment ingest, tier 1 auto or tier 2 approval |
| `auto_categorize` | 12h | learned rules first |
| `anomaly_sentinel` | — | dupes/large debits/price hikes/run-rate |
| `cashflow_alerts` | — | |
| `morning_briefing` | 07:00 | work section, life section, email if SMTP set |
| `custodial_autopilot` | — | prefilled cycle proposal |
| `autopilot` | 05:00 | |
| `monthly_close` | 1st | CFO report + subscription proposals + compliance refresh |
| `capture_digest` | 20:30 | photo-memory day compare, Sunday = weekly rollup |
| `place_learning` | 21:00 | cell×merchant correlation → add_place proposals |
| `commitment_scan` | 08:20 | return-window/warranty detection + deadline ladder |
| `pattern_tasks` | 06:30 | cadence-due merchants → task proposals |
| `relationship_nudges` | 09:00 | broken transfer rhythms, advisory |
| `preference_drift` | monthly 2nd | |
| `meeting_prep_scan` | every 15 min | |
| `connector_sensor_scan` | every 30 min (configurable) | GitHubSensor/PlaneSensor poll |
| `job_scout_poll` | every 12h (configurable) | JobScoutSensor |
| `portfolio_review` | monthly 1st | |
| `career_goal_stall_check` | daily | advisory nudge only |
| `application_followup_check` | every 2 days | one follow-up + auto-ghost after 21 more days |
| `interview_debrief_scan` | hourly | once per calendar event |
| `career_retention` | monthly 3rd | archives 90-day-old unapplied postings |
| `health_bootstrap_check` | 06:05 | L1 — health folder discovery/parse |
| `life_metrics_daily` | 00:30 | L2 — previous day's metrics + L4 day-close habit eval |
| `life_inference_scan` | 10:00 | L3 — 9 inference agents' weekly checks |
| `life_wellbeing_weekly` | 07:15 | L5 — no-ops except Monday |
| `life_review_monthly` | 1st, 06:30 | L6 — monthly Life Review vault note |

## 9. Reactive agents (event-subscribed, `amy/agents/reactive.py`)

| Agent | Kill switch | Trigger | Action |
|---|---|---|---|
| budget | `AMY_AGENT_BUDGET` | finance events | budget suggestion |
| subscription | `AMY_AGENT_SUBSCRIPTION` | finance events | subscription detect proposal |
| compliance | `AMY_AGENT_COMPLIANCE` | ledger events | compliance suggestion |
| screening | `AMY_AGENT_SCREENING` | transaction events | values-screening flag |
| purification | (values) | interest income detected | propose donating the exact amount |
| obligation | `AMY_AGENT_OBLIGATION` | | zakat/tax nudges |
| errand | `AMY_AGENT_ERRAND` | `context.place_entered` | errand suggestion |
| spend_caution | (geo) | `context.place_entered` | caution nudge |
| learning | `AMY_AGENT_LEARNING` | `learning.feed_refreshed`/`item_completed` | goal proposal / stall nudge |
| pr_to_task | `AMY_AGENT_PR_TASK` | `github.pr_review_requested`/changes-requested | `plane_create_task` (external, always tier 2) |
| meeting_prep | `AMY_AGENT_MEETING_PREP` | job-driven (no push event) | vault note + insight, read-only |
| career_goal | (career) | learning-focus trend | propose career goal |
| career_goal_stall_check | (career, job-driven) | daily job | advisory nudge |
| job_scout (`JobScoutSensor`) | `AMY_AGENT_JOB_SCOUT` | poll | discovery + match scoring |
| application tracker | `AMY_AGENT_APPLICATION_TRACKER` | high match score | auto-propose application |
| habit_signals | `AMY_AGENT_LIFE_HABITS` | `context.place_entered/left` | auto-complete/suggest habit check |
| health_bootstrap | `AMY_AGENT_LIFE_HEALTH` | job-driven | targets proposal |
| 9× life inference agents | `AMY_AGENT_LIFE_{COMMUTE,MEALS,SLEEP,ACTIVITY,READING,MEETING_LOAD,ADMIN,SEASONAL,SOCIAL}` | job-driven weekly | propose habit/goal/commitment |
| life_opportunity | `AMY_AGENT_LIFE_OPPORTUNITY` | `context.place_entered` | 12-rule registry, mostly advisory, one tier-0 (`gym_prompt`) |
| meal-capture classifier | `AMY_AGENT_LIFE_CAPTURE_MEALS` | capture ingest | meal calorie estimate |

Master switch `AMY_LIFE_AUTOPILOT` gates every Life Autopilot agent above
regardless of its own switch. Global kill: `POST /api/automation/pause`.

## 10. LLM routing

`AMY_PROVIDER_ORDER=nvidia,openai,groq,ollama` (env). Sensitive data
(anything matched by a jurisdiction pack's sensitivity rules, or explicitly
flagged `sensitive=True`) is forced to Ollama-only, never a cloud key.
Gmail enrichment and budget suggestions batch into a single NVIDIA call.
`use_global_keys=True` is required on finance routes specifically (BYO-key
routes elsewhere use the user's own encrypted key).

## 11. Known quirks worth internalizing

The authoritative, continuously-updated list is `CLAUDE.md`'s "Known
Quirks" section (27 numbered items as of this writing) — read it before any
non-trivial change. Highlights most relevant to planning new features:

- Two different `CareerAgent` classes exist (`agents/folders.py` persona-only
  vs `agents/career.py` wired into `/api/collab/ask`) — don't conflate.
- Agent-gated approvals always have `action_type=="tool_call"`; the real
  tool name is in `payload["tool"]`.
- A new event-emit site that should trigger reactive agents must use
  `amy.events.factory.get_events(...)`, not a bare `EventStore`.
- `FinanceEngine.overview()` reports the most-recent-transaction month, not
  the calendar month (quirk 25) — every other finance method still defaults
  to the real calendar month.
- Recurrence detectors (subscription/investment/income) bucket by
  `(account_id, merchant)`, not merchant alone (quirk 26).
- No Alembic — schema changes are hand-written idempotent `ALTER TABLE`
  blocks per engine.

## 12. Doc map — what's in each file

| File | Covers |
|---|---|
| `CLAUDE.md` | Day-to-day coding reference — architecture, quirks, common patterns |
| `PROJECT_CONTEXT.md` (this file) | Full brain dump for planning conversations |
| `README.md` | Quick start, feature table, deploy notes |
| `API_ENDPOINTS.md` | An older, finance-focused endpoint reference (partially superseded by §6 above) |
| `docs/API_REFERENCE.md` | An older, broader endpoint reference from the pre-SaaS era — verify against §6/the router source before trusting a specific route shape |
| `BUSINESS.md` | Business-entity module design |
| `docs/AGENT_PLAN.md` | Source of truth for agentic-finance project phases/commits |
| `docs/LIFE_AUTOPILOT.md` | Life Autopilot binding spec (hard rules, L1-L9 phase breakdown) |
| `docs/CONTEXT_PLAN.md` | Geo/commitments/patterns/inbox design (C1-C7) |
| `docs/future_enhancements.md` | **Stale** — written for the pre-SaaS PIOS v3 era, predates Finance CFO and every Autopilot system. Superseded by §14 below. |
| `docs/operational_layer_analysis.md` / `docs/operational_layer.md` | Analysis behind the OL removal (see §13) |

---

## 13. Known gaps / honest limitations

These are documented in-code (not bugs — deliberate, disclosed scope cuts).
Useful as a starting menu when asked "what should we build next."

**Stub connectors (honest `available:false`, never fabricated):**
- **Company intel** (`career_apply.py::_company_intel()`) — tries a generic
  `"web_search"` MCP source; none registered by default, so it returns
  `available:false` rather than asking an LLM to guess. Register any
  Brave/Tavily/etc. MCP server named containing `web_search` and it "just
  works" via the existing `call_mcp_tool` resolver.
- **Wearable health data** (`life/health_data.py`) — same pattern, generic
  `"health_data"` MCP source. No built-in wearable connector.
- **Google Cloud Skills Boost** courses source — deliberately omitted (no
  public API, scraping is ToS-risky).
- **Calendar-derived signals** — `life_metrics.meeting_count`/`focus_blocks`
  stay `None` permanently; no calendar-day-range helper exists yet. This is
  a real, named gap in the Life Autopilot meeting-load inference agent.

**Permanent no-op rules (by design, not bug):**
- `person_proximity` opportunity rule — no person↔place association model
  exists anywhere in the codebase.
- `pharmacy` opportunity rule — was a no-op until `life/commitments_life.py`
  (L8) started producing refill commitments; now live end-to-end.

**Structural gaps:**
- GitHub/Plane sensors don't filter "assigned to me" against the
  authenticated identity — any non-empty assignee/reviewer list counts.
  Fine for single-user-per-connector; would need a `get_me` lookup if a
  connector is ever shared.
- No Alembic/real migration tool — schema changes are hand-rolled
  idempotent `ALTER TABLE` calls per engine's `_migrate()`.
- Investments have no live price feed — `current_value` defaults to
  `cost_basis` (sum of contributions seen), never updates on its own.
- Portfolio analyst's SHOWCASE/NEEDS-WORK/NOT-RELEVANT classification isn't
  persisted anywhere queryable outside its vault note — job-match scoring's
  "portfolio evidence" factor is inferred from `career_profile.skills` only,
  not the actual classification.
- Three separate retrieval/RAG code paths reportedly exist historically
  (`index.py` Chroma / `knowledge` cosine / `pkos` hybrid per
  `docs/future_enhancements.md`) — worth re-auditing whether this is still
  true before consolidating; that doc predates the SaaS pivot.
- Two knowledge-graph-shaped things exist: `amy/knowledge_graph/store.py`
  (cross-source, `graph.db`) vs `amy/knowledge/relationships.py` (notes-only,
  wiki-links + keyword overlap) — not a bug, but a naming trap.
- Chat streaming (`/api/collab/ask/stream`) is progressive (status → full
  answer), not true token-level provider streaming.
- No billing/quotas/metering layer — deliberately deferred, would slot in
  as middleware on existing endpoints if ever needed for multi-tenant SaaS.
- Passwords use PBKDF2, not Argon2/bcrypt.
- In-process event bus + scheduler — no task queue (Celery/RQ/Arq); fine
  for a single-instance deployment, would need rework for horizontal scale
  or true multi-tenant HA.

## 14. Ideas for future enhancement

Grouped by theme, grounded in the gaps above and the existing architecture
(each idea names the layer it would slot into, since almost everything here
should reuse the tool-registry/event-bus/AGENT_GATE/approval-inbox spine
rather than growing a parallel mechanism).

**Finance depth**
- Live investment pricing: a price-feed connector (mirrors the
  `company_intel`/`health_data` "generic MCP source, honest unavailable"
  pattern) so `investments.current_value` stops being a static sum.
- Multi-currency net worth rollup surfaced on the dashboard, not just
  `/api/finance/overview/fx` on request — the jurisdiction/currency
  plumbing (quirk-adjacent columns already exist on `accounts`/
  `transactions`/`business_entities`) is already there, just not visualized.
- Extend the subscription/investment/income "account-scoped bucketing" fix
  (quirk 26) into the anomaly sentinel and budget-suggestion paths if they
  independently bucket by merchant name — worth a quick audit for the same
  cross-account-merge bug elsewhere.

**Career Autopilot**
- A calendar-derived interview/offer timeline view, once the calendar-range
  helper (currently missing — see §13) is built for Life Autopilot's
  meeting-load agent; the two features can share it.
- Persist portfolio classification (SHOWCASE/NEEDS WORK/NOT RELEVANT) so
  job-match scoring's "portfolio evidence" factor becomes real instead of
  skills-inferred.
- Wire the company-intel and wearable-health stubs to a concrete MCP server
  once the user picks one (Brave/Tavily for the former; an Oura/Fitbit MCP
  bridge for the latter) — the resolver side is already generic.

**Life Autopilot**
- Real calendar signal source for `meeting_count`/`focus_blocks` — the
  single most-cited "known gap" in the Life Autopilot code comments.
  Building it unblocks the meeting-load inference agent.
- A device sleep-data connector (see health_data.py stub) would upgrade
  `sleep_provenance` from `'inferred'` to `'device'` for users with a
  wearable, tightening every downstream wellbeing/habit calculation that
  depends on sleep.

**Cross-cutting infrastructure**
- Real DB migrations (Alembic or a lightweight hand-rolled versioned
  migration runner) — every per-user SQLite file currently self-migrates
  via ad hoc `ALTER TABLE` try/except blocks; fine at today's scale, a risk
  if schema changes get more frequent or the user base grows.
- Consolidate the three historically-separate retrieval paths (worth
  re-verifying this is still 3 and not already merged before scoping work).
- A lightweight per-user "what changed since you last looked" digest that
  spans Finance + Career + Life + Learning in one place — the ingredients
  (events table, activities table, notifications table) all already exist;
  today each Autopilot writes its own vault note/briefing section
  independently rather than one unified feed.

**UX**
- Surface `/api/connectors/status`'s health dot pattern (green/amber/red)
  more prominently — it already exists but per `CLAUDE.md` was recently
  added and may be under-visible in the current nav.
- True token-level chat streaming (the current `/api/collab/ask/stream` is
  progressive, not per-token) — mostly a provider-plumbing change in
  `LLMRouter`.

---

_When starting a new planning conversation: paste this file, then name the
specific module/feature you want to discuss. For anything touching a table
or route listed above, cross-check the live source before committing to an
implementation — this file is a map, not the territory._
