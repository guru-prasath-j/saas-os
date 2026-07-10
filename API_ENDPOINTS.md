# Amy PersonalOS — API Endpoints Reference

All endpoints require `Authorization: Bearer <token>` header (obtained from `/auth/login`).

Base URL: `http://localhost:8849`

---

## Auth

| Method | Path | Description |
|--------|------|-------------|
| POST | `/auth/login` | Sign in — returns `{"token": "..."}` |
| POST | `/auth/signup` | Create account |
| GET | `/api/me` | Current user info (email, has_openai_key) |
| POST | `/api/me/key` | Save OpenAI API key |
| GET | `/api/me/privacy` | Get private folder prefixes |
| POST | `/api/me/privacy` | Save private folder prefixes |
| GET | `/api/vault` | Vault stats (note count, last import) |
| POST | `/api/vault/settings` | Save vault path (cloud/local toggle) |
| GET | `/api/vault/settings` | Load vault path settings |

---

## Finance CFO

### Transactions
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/finance/transactions` | List transactions (filters: search, category, account_type, since, until) |
| POST | `/api/finance/transactions` | Add a single transaction |
| DELETE | `/api/finance/transactions` | **Reset** — delete ALL transactions (keep accounts) |
| DELETE | `/api/finance/transactions/{tid}` | Delete one transaction |
| POST | `/api/finance/transactions/auto-categorize` | Run rule-based categorizer on all Uncategorized |
| PATCH | `/api/finance/transactions/{tid}/category` | Update category of one transaction |

### Overview & Forecast
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/finance/overview` | Summary cards: income, expenses, balance, top categories |
| GET | `/api/finance/forecast/cashflow` | Next-week cashflow prediction |
| POST | `/api/finance/afford` | "Can I afford this?" — body: `{amount, description}` |
| GET | `/api/finance/goals` | Financial goals derived from budgets + income |

### Accounts
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/finance/accounts` | List all accounts |
| POST | `/api/finance/accounts` | Add account — body: `{nickname, bank_name, account_type}` |
| DELETE | `/api/finance/accounts` | Delete all accounts |
| DELETE | `/api/finance/accounts/{aid}` | Delete account + all its transactions |
| GET | `/api/finance/accounts/{aid}/transactions` | Transactions for one account |

### Import — CSV / XLS / XLSX
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/finance/accounts/{aid}/preview/csv` | Parse CSV/XLS — returns rows, **no DB write** |
| POST | `/api/finance/accounts/{aid}/upload/csv` | Parse + save CSV/XLS to DB |
| POST | `/api/finance/accounts/{aid}/column-map` | Save manual column mapping for this bank |
| GET | `/api/finance/bank-presets` | Named bank presets (HDFC, ICICI, SBI…) |
| GET | `/api/finance/column-maps` | Saved column maps per bank |

### Import — PDF
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/finance/accounts/{aid}/preview/pdf` | Parse PDF — returns rows, **no DB write** |
| POST | `/api/finance/accounts/{aid}/upload/pdf` | Parse + save PDF to DB |

### Gmail Sync
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/finance/sync/gmail` | Global sync — all savings/current accounts. Params: `since`, `until`, `max_messages` |
| POST | `/api/finance/accounts/{aid}/sync/gmail` | Per-account Gmail sync (legacy) |
| GET | `/api/finance/gmail/scope-status` | Check if Gmail OAuth scope is active |

### Deduplication
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/finance/duplicates` | Scan and return duplicate groups (exact / near / fuzzy) |
| POST | `/api/finance/duplicates/resolve` | Delete selected duplicates — body: `{delete_ids: [...]}` |
| DELETE | `/api/finance/duplicates/auto` | Auto-remove all exact duplicates (keeps oldest) |

### Budgets
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/finance/budgets` | List budgets with spending vs limit |
| POST | `/api/finance/budgets` | Set budget — body: `{category, monthly_limit}` |
| DELETE | `/api/finance/budgets` | Delete all budgets |

### Subscriptions
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/finance/subscriptions` | List subscriptions |
| POST | `/api/finance/subscriptions` | Add subscription — body: `{name, amount, billing_cycle, next_due, category}` |
| PATCH | `/api/finance/subscriptions/{sid}` | Update subscription |
| DELETE | `/api/finance/subscriptions/{sid}` | Delete subscription |

### Investments
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/finance/investments` | List investments with total value + P&L |
| POST | `/api/finance/investments` | Add investment — body: `{type, name, current_value, cost_basis}` |
| PATCH | `/api/finance/investments/{iid}` | Update investment |
| DELETE | `/api/finance/investments/{iid}` | Delete investment |

### Income Sources
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/finance/income` | List income sources |
| POST | `/api/finance/income` | Add income source — body: `{name, amount, frequency}` |
| DELETE | `/api/finance/income/{id}` | Delete income source |

### Calendar
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/finance/calendar/sync` | Push bill due-dates & subscription renewals to Google Calendar |

---

## Knowledge (Vault / Memory Lake)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/search` | Full-text + semantic search — param: `q` |
| POST | `/api/vault/import` | Import Obsidian vault ZIP |
| GET | `/api/vault` | Vault stats |
| POST | `/api/knowledge/build` | Rebuild knowledge graph from vault notes |
| GET | `/api/knowledge/graph` | Graph data (nodes + edges) for visualization |
| GET | `/api/knowledge/tags` | All tags in vault with counts |
| GET | `/api/knowledge/folders` | Folder tree |
| GET | `/api/knowledge/files` | All files with metadata |
| GET | `/api/knowledge/file` | Read one file — param: `path` |

---

## Memory & Journal

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/memory/summary` | AI-generated memory summary across all notes |
| GET | `/api/memory/entities` | Extracted people, places, orgs, events |
| POST | `/api/memory/sync` | Sync memory from latest notes |
| GET | `/api/memory/journal` | Journal entries |
| POST | `/api/memory/journal` | Add journal entry |
| GET | `/api/memory/reindex` | Rebuild memory index |

---

## Goals

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/goals` | List goals with milestones |
| POST | `/api/goals` | Create goal — body: `{title, domain, target_date}` |
| POST | `/api/goals/{gid}/milestone` | Add milestone to a goal |
| PATCH | `/api/goals/{gid}/milestone/{mid}` | Toggle milestone done |
| DELETE | `/api/goals/{gid}` | Delete goal |

---

## Habits

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/habits` | List habits + streak data |
| POST | `/api/habits` | Add habit — body: `{name, frequency}` |
| POST | `/api/habits/{hid}/log` | Log today's completion |
| DELETE | `/api/habits/{hid}` | Delete habit |

---

## Decisions

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/decisions` | Decision history |
| POST | `/api/decisions` | Log decision — body: `{title, reason, category}` |
| GET | `/api/decisions/analysis` | Pattern analysis of past decisions |
| GET | `/api/decisions/recommendations` | AI recommendations based on decision history |

---

## Timeline

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/timeline` | Life events timeline — param: `range` (day/week/month) |

---

## Intelligence (Digital Twin & AI)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/intelligence/twin` | Digital twin summary |
| GET | `/api/intelligence/personality` | Personality profile derived from notes |
| GET | `/api/intelligence/predict` | Predictions for next week/month |
| POST | `/api/intelligence/future-self` | Validate decision against long-term goals — body: `{title, category}` |
| POST | `/api/intelligence/autopilot` | Run autonomous planning cycle |

---

## Agents

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents` | List available domain agents + enabled status |
| POST | `/api/agents/{name}/toggle` | Enable or disable an agent |
| POST | `/api/master` | Send message — multi-agent routing, returns AI response with sources |

---

## Learning & Review

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/intelligence/learn` | Learning trend analysis from notes |
| GET | `/api/intelligence/reflect` | Weekly reflection summary |
| GET | `/api/srs/cards` | Due flashcards (spaced repetition) |
| POST | `/api/srs/review` | Submit card review — body: `{card_id, rating}` |

---

## People & Entities

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/entities` | All extracted people, places, organisations |
| GET | `/api/entities/{name}` | Entity detail with all mentions |

---

## Portfolio (Public Profile)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/product/portfolio` | Public portfolio data (projects, skills — no private data) |
| GET | `/api/product/suggestions` | AI suggestions for improving profile |

---

## Connectors (Google OAuth)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/connectors/google/status` | Check if Google account is connected |
| GET | `/api/connectors/google/auth-url` | Get OAuth redirect URL |
| GET | `/api/connectors/google/callback` | OAuth callback (redirect target) |
| POST | `/api/connectors/google/sync` | Sync Google Calendar + Tasks |
| DELETE | `/api/connectors/google/disconnect` | Revoke Google access |

---

## Connector Health (CONNECTOR COMPLETION Part 3)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/connectors/status` | Every connector's health: Google services (Gmail/Calendar-Meet/Sheets), local MCP servers (jobspy/HackerNews/YouTube/Dev.to — supervisor state), external MCP connectors (GitHub/Plane/…). No live calls — reads the `connector_calls` ledger + registered rows. |

Registry tools (not REST routes — consumed by agents/the orchestrator via
`amy.tools.invoke`, see `amy/tools/connector_tools.py`):

| Tool | Risk | Notes |
|------|------|-------|
| `github_list_prs` / `github_list_issues` / `github_pr_details` | read | Against the registered `github` MCP connector |
| `plane_list_tasks` / `plane_task_details` | read | Against the registered `plane` MCP connector |
| `meet_upcoming_meetings` | read | Google Calendar directly (not MCP) |
| `github_comment` | write, **external** | Always tier 2 — `AMY_AGENT_WRITE_TIER` cannot soften it |
| `plane_create_task` / `plane_update_task` | write, **external** | Always tier 2 |

---

## Career Autopilot

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/career/profile` | Target role/location/remote/deadline/skills + active career goal (progress %). Never returns raw `resume_text`. |
| PUT | `/api/career/profile` | Update the career profile; `resume_text` is stored Fernet-encrypted. |
| GET | `/api/career/postings` | Discovered job postings (`?status=&limit=`), sorted by match score. |
| GET | `/api/career/applications` | Applications + funnel counts (`?status=`). |
| PATCH | `/api/career/applications/{id}` | Record a real-world outcome (`status`, `note`) — human-reported, writes directly, not agent-gated. |
| GET | `/api/career/portfolio` | Runs the portfolio analyst live (SHOWCASE/NEEDS WORK/GAPS) — has side effects (may propose a gap-project approval, always writes a vault note) despite being a GET; idempotent per day. |
| POST | `/api/career/postings/{id}/apply` | Prepares an application (channel/ATS/company-intel/draft) and parks ONE approval — never sends anything itself. |

Registry tools (consumed by agents/the orchestrator via `amy.tools.invoke`,
see `amy/tools/career_tools.py`):

| Tool | Risk | Notes |
|------|------|-------|
| `job_search` | read | Wraps the jobspy MCP connector's `search_jobs` |
| `job_details` | read | Local `job_postings` lookup — no live call |
| `portfolio_repo_list` / `portfolio_repo_details` | read | Against the registered `github` MCP connector |
| `career_status` | read | Goal + plan progress + funnel counts |
| `set_career_profile` | write | Internal |
| `application_log` | write | Internal — create/update an application's status |
| `send_hr_email` | write, **external** | Always tier 2 — SMTP if configured, else a copy-ready draft |
| `plane_batch_create_tasks` | write, **external** | Always tier 2 — ONE approval creates every task atomically |

Jobs: `job_scout_poll` (default 12h, `AMY_JOB_SCOUT_INTERVAL_HOURS`),
`portfolio_review` (monthly), `career_goal_stall_check` (daily),
`application_followup_check` (every 2 days).

Kill switches: `AMY_AGENT_CAREER_GOAL`, `AMY_AGENT_PORTFOLIO`,
`AMY_AGENT_JOB_SCOUT`, `AMY_AGENT_APPLICATION_TRACKER`.

## Life Autopilot (L1-L2)

Full spec: `docs/LIFE_AUTOPILOT.md`.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/life/metrics` | `?from=&to=` (defaults to the trailing 30 days) — `life_metrics` rows: office/commute/gym/sleep/meals/day_type/grace per day. Read-only. |
| POST | `/api/life/habits/{habit_id}/link` | Create a `habit_links` row — body `{signal_type, signal_params, mode}`. |
| GET | `/api/life/habits/{habit_id}/links` | List links for a habit. |
| DELETE | `/api/life/habit-links/{link_id}` | Remove a link (habit stays fully manual again). |
| GET | `/api/life/habits/link-suggestions` | `?title=` — keyword-matched signal suggestion for the Add-habit flow, or `null`. Suggestion only, never forced. |

Registry tools:

| Tool | Risk | Notes |
|------|------|-------|
| `health_targets` | read | Computed BMR/TDEE/sleep-band/protein/water from `health_profile`; `available:False` (never fabricated) with an incomplete profile |
| `complete_habit_check` | write | Human/chat-assistant use; the habit_links auto-completion mechanism bypasses this tool entirely (calls the executor directly for tier 0/1) |
| `adjust_habit_target` | write | Adjusts a habit's grace-per-week override; always tier 2 with an old→new diff when agent-invoked |

Jobs: `health_bootstrap_check` (06:05 — finds/parses the health vault
folder, proposes targets, polls for vault re-parse), `life_metrics_daily`
(00:30 — computes the previous day's `life_metrics` row, then runs
day-close habit-link evaluation + adaptation checks, idempotent).

Kill switches: `AMY_AGENT_LIFE_HEALTH`, `AMY_AGENT_LIFE_HABITS`. Master
switch: `AMY_LIFE_AUTOPILOT`.

Backfill: `python -m amy.life.backfill <email> <start-date> <end-date>`.

---

## Events

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/events` | Calendar events |
| POST | `/api/events` | Add event |
| DELETE | `/api/events/{eid}` | Delete event |

---

## Notifications

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/notifications` | Recent notifications |
| POST | `/api/notifications/mark-read` | Mark all read |

---

## Account Aggregator (AA)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/connectors/aa/status` | AA connection status |
| POST | `/api/connectors/aa/toggle` | Enable/disable AA data access |

---

## LLM Routing

Amy routes LLM calls automatically:

| Provider | Model | Used For |
|----------|-------|----------|
| NVIDIA NIM | `nvidia/nemotron-3-ultra-550b-a55b` | Gmail enrichment, PDF parsing, primary chat |
| OpenAI | `gpt-4o-mini` | Per-user key fallback |
| Groq | `llama-3.3-70b-versatile` | Secondary fallback |
| Ollama | `llama3.2` (local) | Sensitive/private data — never leaves device |
| Template | deterministic | Last-resort fallback (always works) |

**Rule:** Notes marked `sensitive` (private folders) → Ollama only, never cloud.

---

## Automation Layer

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/automation/status` | Paused flag, all jobs with next/last run, pending approvals count |
| GET | `/api/automation/jobs` | List scheduled jobs |
| PATCH | `/api/automation/jobs/{name}` | Enable/disable or reschedule — body: `{enabled?, schedule?}` |
| POST | `/api/automation/jobs/{name}/run` | Run one job immediately |
| GET | `/api/automation/runs` | Run ledger — params: `job`, `limit` |
| GET | `/api/automation/approvals` | Approval Inbox — param: `status=pending\|all\|executed\|rejected\|expired` |
| POST | `/api/automation/approvals/{aid}/approve` | Execute a pending approval (recorded in DecisionEngine) |
| POST | `/api/automation/approvals/{aid}/reject` | Reject — body: `{reason}` |
| GET | `/api/automation/llm-stats` | Per-provider call counts / success / latency |
| GET | `/api/automation/dead-letters` | Event subscribers that failed twice |
| GET | `/api/automation/learned-rules` | Learned categorizer rules |
| POST | `/api/automation/pause` / `resume` | Global automation kill switch |
| POST | `/api/assistant/chat` | Tool-loop assistant — body: `{message, history?}` |

Jobs added by CONNECTOR COMPLETION: `meeting_prep_scan` (every 15 min —
drives the read-only `meeting_prep` agent's calendar-window check) and
`connector_sensor_scan` (interval via `AMY_CONNECTOR_SENSOR_INTERVAL_HOURS`,
default 30 min — polls `GitHubSensor`/`PlaneSensor`; also what the
Connectors tab's "Sync now" button triggers for GitHub/Plane). Both run via
the existing `POST /api/automation/jobs/{name}/run` for a manual trigger.

## Agent (orchestrator + audit)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/agent/goal` | Natural-language goal → plan → gated tool calls — body: `{goal}` |
| GET | `/api/agent/goals` | Past orchestrator runs (plan, steps, summary) |
| GET | `/api/agent/audit` | Regulator-style report — params: `from`, `to` (events, runs, approvals w/ reasoning, decisions, screening flags, LLM-routing docs) |

## Jurisdictions & Locale (R7B)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/jurisdictions` | Available packs + user's home/active |
| GET | `/api/jurisdictions/deadlines` | Upcoming obligation/compliance dates across active packs — param: `days` |
| GET | `/api/jurisdictions/{pack_id}` | Full pack JSON |
| GET/POST | `/api/settings/locale` | `{home_jurisdiction, active_jurisdictions[], language, currency}` |
| GET | `/api/finance/overview/fx` | Per-currency + per-jurisdiction totals converted to base currency |

## Obligations (R7A-2)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/obligations` | Active obligations with computed status, rules shown, disclaimer |
| GET | `/api/obligations/presets` | Presets available across active jurisdictions |
| POST | `/api/obligations/activate` | Body: `{jurisdiction, preset_id, config}` (e.g. `estimated_annual_amount`) |
| PATCH | `/api/obligations/{oid}` | Update config — body: `{config}` |
| POST | `/api/obligations/{oid}/deactivate` | Pause an obligation |

## Values Screening (R7A-1)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/values/presets` | Shipped rule presets (interest_free_finance, esg_basic, budget_discipline) |
| GET | `/api/values/profiles` | User's editable profiles |
| POST | `/api/values/profiles` | Enable a preset or create custom — body: `{preset_id}` or `{name, rules}` |
| PATCH | `/api/values/profiles/{pid}` | Body: `{enabled?, rules?}` |
| GET | `/api/values/flags` | Screening flags — param: `status=open\|all` |
| POST | `/api/values/flags/{fid}/dismiss` | Dismiss a flag |

Notes:
- `POST /api/finance/afford` accepts optional `financing_months` (+ `financing_annual_rate`) → response gains `financing_options` compared across the models the user's jurisdiction pack enables.
- `DELETE /api/finance/transactions` (full wipe) requires `?confirm=DELETE-ALL-TRANSACTIONS`.

---

## Running the Server

```bash
# Start
python -m uvicorn amy.saas.app:app --host 0.0.0.0 --port 8849

# Kill existing (Windows)
Get-NetTCPConnection -LocalPort 8849 | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

Environment: copy `.env.example` to `.env` and fill in API keys.
