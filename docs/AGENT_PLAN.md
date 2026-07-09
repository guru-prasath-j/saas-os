# AGENT_PLAN.md — Approved implementation plan (source of truth)

> If session context is lost: read this file + CLAUDE.md, then continue from
> the first phase not marked DONE. Update this file as phases complete
> (mark DONE with commit hash).

## Mission

Evolve Amy PersonalOS into an AI-native, trust-first agentic finance platform
(Stage 1 analysis inspired by Mal-style ethical banking) — **fully generic**:
no hardcoded references to any religion, company, or jurisdiction in Python
code. Presets/rules live in data/config files and must demonstrably work for
UAE, US, and India from day one; a 4th jurisdiction = one new JSON pack only.

## Build order (approved)

R1 → R3 → R2 → R7A-6 → R4 → R7A-3 → R7B → R7A-2 → R7A-1 → R7A-4 → R5.
**R6 (sensors + MCP server) is DEFERRED — do not build.**
Commit after each phase. Present a short file-mapped plan before each phase.

## Progress

| Step | Description | Status | Commit |
|---|---|---|---|
| 0.1 | Commit pre-existing automation layer as rollback boundary | DONE | 900c539 |
| 0.2 | This plan file | DONE | dbcd716 |
| 0.3 | Security: JWT secret ≥32B + gate DELETE all-transactions | DONE | 32f3c05 |
| R1 | Tool registry (amy/tools/) | DONE | d10672f |
| R3 | Unified approval queue (extend existing approvals) | DONE | 8a33642 |
| R2 | Reactive agents on the event bus | DONE | 7d2d2f2 |
| R7A-6 | Audit export | DONE | ba52cdf |
| R4 | Orchestrator agent | DONE | 16b0a40 |
| R7A-3 | Calendar abstraction | DONE | 0061f6c |
| R7B | Jurisdiction packs + FX + locale | DONE | 53060e9 |
| R7A-2 | Obligations engine | DONE | 1f2a064 |
| R7A-1 | Values screening engine | DONE | 5b528d0 |
| R7A-4 | Financing-model interface | DONE | e4b13e9 |
| R5 | Briefing upgrades (final integration) | DONE | 7b390d1 |

**ALL PHASES COMPLETE** (R6 deferred by decision). Docs updated in the
final commit: API_ENDPOINTS.md (5 new sections), CLAUDE.md (layout, agent
events, quirks 15–19), docs/jurisdictions.md.

## Post-launch bug fixes (found during manual UI testing)

Two real bugs surfaced running the orchestrator against real data via the
browser (goal: "cut my spending 10%"):

1. **Custodial category blindness** — the orchestrator proposed cutting the
   "Custodial Disbursement" budget by 10%, treating pass-through money
   forwarded to beneficiaries as if it were the user's own discretionary
   spending. Fixed: `amy/tools/builtin.py` adds `is_custodial_category()`
   (>=90% of a category's transaction volume from custodial accounts);
   `list_budgets` now returns `custodial_category` per row and both
   `list_budgets`/`set_budget` tool descriptions warn against it;
   `amy/automation/executors.py`'s `agent_gate` injects a visible ⚠️
   warning into the approval card regardless of whether the LLM heeded the
   description. Read-only checks — custodial.py itself untouched.
2. **No dedup on orchestrator proposals** — running an equivalently-worded
   goal twice ("cut spending 10%" vs "reduce spending by 10 percent")
   queued two separate pending approvals for the identical action. Fixed:
   `amy/automation/orchestrator.py` now computes a dedup key
   (tool name + sorted args hash) before every tool invocation, matching
   what reactive agents already do — a repeat proposal collapses into the
   existing pending one, but a fresh proposal is still allowed after
   rejection (dedup only blocks pending/executed rows).

Tests: `tests/test_manual_testing_bugfixes.py` (8 passing). Full suite
re-verified: 508 passed; 18 pre-existing failures (categorizer/BYOK/
career-agent/finance-import tests, none touching the files changed here)
confirmed present on the pre-fix baseline too — unrelated to this work.

## Approved decisions

1. **Approval queue**: EXTEND the existing `approvals` table + tier router +
   `/api/automation/approvals/*` (amy/automation/store.py, executors.py).
   Do NOT create a separate `pending_actions` table.
2. Build order as above (packs before obligations so presets load from packs).
3. R6 deferred.

## Global constraints (from CLAUDE.md — every phase)

- All LLM calls via `LLMRouter`; sensitive → `pick(sensitive=True)` → Ollama-only.
- Events via existing `EventStore`, fire-and-forget (`_emit_fin` style);
  agent failures must NEVER break API routes.
- New subscribers in `amy/events/triggers.py` style; no new bus.
- Custodial accounts (`account_type='custodial'`) excluded from
  income/spend/obligation math everywhere; never touch custodial disbursement
  or SBI logic.
- FastAPI route order: exact before parameterized. Restart uvicorn after
  adding routes (no hot reload for routes).
- Frontend = one file `amy/saas/static/index.html`; new UI as `data-tab`
  tabs/panels matching existing patterns, via the `api()` JS helper.
- Per-user SQLite via existing helpers (`_finance_db`, `paths.py`); new
  tables created idempotently.
- MemoryWriter journaling stays idempotent.
- `config.py` env flags; `.env.personal` loads first with override=False
  (new vars go to `.env`).

## Cross-cutting requirements (from Phase R1 onward)

- Every agent decision stores an explicit reasoning string linked to its
  event/run.
- Add `agent.*` entries to MemoryWriter `_KIND` so agent activity journals
  to the vault.
- Error norm: run ledger + event dead-letters; agents report errors as
  events; **no bare `except: pass` in new code**.
- Per-user "local-only" LLM routing flag: when set, ALL that user's LLM
  calls route to Ollama regardless of sensitivity classification.
- Per-agent kill switch: `AMY_AGENT_<NAME>=0/1` in config.py, default ON
  except destructive-capable agents.
- No hardcoded religion/company/jurisdiction in Python code.

## Phase specs

### Step 0.3 — Security fixes (own commit)
(a) JWT signing key ≥32 bytes (fix InsecureKeyLengthWarning): honor
`AMY_JWT_SECRET` if strong; stretch short env secrets via SHA-256; if unset,
auto-generate once and persist under saas_data (tokens survive restarts).
Document the env var. (b) `DELETE /api/finance/transactions` (full wipe)
requires explicit confirmation token — no single-call wipe. Update the
frontend caller.

### R1 — Tool registry (`amy/tools/`)
Formal registry replacing the assistant's hardcoded TOOLS dict. Each tool:
name, description, JSON schema for params, handler, risk level
(`read` | `write` | `destructive` = money-affecting/deleting/external sends).
Wrap: FinanceEngine (transactions, budgets, income, subscriptions),
`afford.can_afford`, business ledger + compliance, vault write via
MemoryWriter, GraphStore queries, calendar, `EventStore.emit`.
`amy/automation/executors.py` is the execution backend for write/destructive.
Assistant consumes this registry.

### R3 — Unified approval queue (extend, don't duplicate)
- Add `expires_at` + affected-entity fields to `approvals`.
- Any registry write/destructive tool invoked BY AN AGENT parks in the queue
  (tool, params, reasoning, risk, affected entity). Human-invoked UI actions
  stay direct. Approved actions execute through the registry; approve/reject
  recorded in DecisionEngine.
- Frontend: new `data-tab="agent"` tab with pending approvals + reasoning +
  approve/reject.
- Tier policy becomes explicit config (replace execute-then-notify defaults
  for agent-initiated writes).

### R2 — Reactive agents on the event bus
Subscribers (triggers.py style) for finance.gmail_synced, finance.csv_imported,
finance.subscription_added, finance.ledger_entry_posted:
- Budget agent: re-check caps via `budget_status()` after imports; emit
  `agent.insight` with reasoning; journal via MemoryWriter.
- Subscription agent: run `subscription_detect` proactively; emit suggestions.
- Compliance agent: on ledger_entry_posted evaluate compliance/run
  (respect `tracking_closeness` gate).
All actions emit `agent.*` events with reasoning; rely on retry-once +
dead-letter isolation.

### R7A-6 — Audit export
`GET /api/agent/audit?from=&to=` — regulator-style report joining events,
automation runs, approvals, decisions, and (later) screening flags: every
agent action, reasoning, approval/rejection, provenance links. Report
metadata documents LLM routing (which providers can see what).

### R4 — Orchestrator agent
`POST /api/agent/goal` (natural-language goal). Grow from
`amy/automation/assistant.py` (multi-step JSON loop, provider-timeout
handling, first-JSON-object parse quirk). LLMRouter plans tool calls from R1;
`ContextModule.get_context()` for awareness; read tools direct;
write/destructive via R3 queue; step results feed back; plan→steps→outcomes
as GraphStore nodes/edges (goal/task types; depends_on/belongs_to);
summary journaled. Frontend: goal input + plan/progress view in agent tab.

### R7A-3 — Calendar abstraction
"What period is date X in under calendar system Y (gregorian | hijri |
fiscal[start month configurable]) and when does it end?" Use hijri-converter.
Consumed by obligations + briefings. No holiday hardcoding (packs carry named
dates as data). New calendar system = one adapter class; new jurisdictions on
existing calendars = JSON only.

### R7B — Jurisdiction packs (BINDING SPEC — may extend, not weaken)
- `amy/jurisdictions/{uae,us,india}.json`; no jurisdiction logic in Python.
  Pack defines: currency code + number formatting (incl. lakh/crore), fiscal
  year start, calendar systems, obligation presets (rates/deadlines/
  thresholds), compliance deadline calendar, enabled financing models,
  default screening profiles. Effective-date versioning on rates/dates
  (extend rate_table's effective_from/effective_to pattern).
- Packs: UAE (AED, corporate tax 9% above threshold, VAT 5%,
  Gregorian+Hijri, zakat & interest_free_finance presets), US (USD, IRS
  quarterly estimated dates, annual filing deadline, retirement contribution
  preset, calendar FY), India (INR lakh/crore, GST awareness, advance tax
  installments, ITR deadline, Apr–Mar FY).
- User model: home_jurisdiction + active list; accounts + business entities
  get optional jurisdiction (default home). Obligations/compliance/deadlines
  computed per jurisdiction from pack calendar + rules. Rate-grounding rule
  ("never from LLM training data") extends to pack data.
- Multi-currency: native currency per account/transaction; FX module
  (pluggable source, cached daily, mockable); dashboards + afford in base
  currency with per-jurisdiction breakdowns.
- Locale layer folded in: per-user output language, currency display, number
  grouping — passed to LLM prompts + UI formatting; fix hardcoded ₹ in
  context.py and index.html. Sensitive routing unaffected.
- `docs/jurisdictions.md` with copyable template proving pack #4 = JSON only.
- Disclaimers: estimates, not professional tax advice, rules/dates shown
  (mirror CA disclaimer pattern).

### R7A-2 — Obligations engine
`ObligationRule = {rate, wealth_threshold, holding_period, calendar_system,
eligible_account_types (custodial ALWAYS excluded), schedule}` loaded from
packs; per-user activation in per-user DB. Presets: zakat (2.5%, nisab,
lunar year, hijri), quarterly_tax_estimate (US), advance_tax (India),
savings_commitment (proves non-religious generality). Obligation agent
tracks accrual from FinanceEngine, computes liability, surfaces in
briefings, proposes payments ONLY via approval queue.

### R7A-1 — Values screening engine (`amy/values/`)
ValuesProfile = data object (flagged merchant categories, transaction
attributes, financing types) — never an if-religion-then branch. Presets:
interest_free_finance, esg_basic, budget_discipline (purchase >X% of
monthly income). Screening agent on new-transaction events
(categorizer-shaped: rules → optional LLM reasoning, sensitivity intact),
flags with reasoning, remediations via approval queue. Per-user profiles,
API + settings panel. Flags appear in audit export.

### R7A-4 — Financing-model interface
Strategies: amortized_interest, profit_rate_markup,
installment_zero_interest, lease_to_own — total cost + schedule from
{principal, term, rate/markup}. `can_afford()` optional comparison across
enabled models. Enabled set from packs + values profile. New model = new
strategy class registered by name.

### R5 — Briefing upgrades (final integration)
Extend morning_briefing + digest: R2 insights, obligation statuses,
multi-jurisdiction deadline calendar, currency-converted totals with
per-jurisdiction breakdowns, renewals next 7 days, pack-defined seasonal
awareness. Locale-rendered. Env-configurable schedule. Journaled; latest
briefing on dashboard.

## Tests & docs (throughout)

Tests in `tests/`: registry schema + risk gating; approval lifecycle
(park→approve→execute→decision; reject; expiry); one reactive agent flow
(event → agent → journal); calendar period math (3 systems); obligations
(≥1 preset per jurisdiction); FX conversion; values screening flag;
financing total-cost math.
Update API_ENDPOINTS.md + CLAUDE.md as phases land; docs/jurisdictions.md;
keep this file's Progress table current.

---

## Phase: CONNECTOR COMPLETION

Completes the remaining connector work after the learning-feed pipeline +
local MCP servers (HN/YouTube/Dev.to) landed: GitHub + Plane integration,
Meet/calendar-driven meeting prep, the connectors health tab, and a
structural fix for quirk 20 (every EventStore emit site having to remember
`register_reactive_agents` itself).

### Progress

| Part | Description | Status | Commit |
|---|---|---|---|
| 0 | `amy/events/factory.py` + idempotent registration + zero-subscriber warning; migrate known emit sites | DONE | 41aec45 |
| 1 | GitHub + Plane registry tools (read + external-pinned write) + connector_calls ledger | DONE | dd7fc24 |
| 2 | Sensors (GitHubSensor/PlaneSensor) + reactive agents (pr_to_task, meeting_prep) + jobs (project_pulse, meeting_prep_scan) | DONE | ba4a863 |
| 3 | `/api/connectors/status` + Connectors tab (index.html) | DONE | 2a5355d |

### Part 0 — structural fix for quirk 20 (DONE)

`amy/events/factory.py::get_events(user_id, collab_db, index_dir=None,
user_email="", ctx=None)` — the one place that builds an `EventStore` with
reactive agents wired on. Lazy-imports `agents.reactive`/`automation.jobs`
inside the function body (RISK A: no `events → agents.reactive → tools →
automation → events` cycle — verified via an isolated
`python -c "import amy.events.factory"` subprocess test AND a normal app
cold-import). RISK B (double registration → double-fire) fixed at the
`EventStore` level: `_registered_agent_keys` tracks agents already wired on
an instance; `register_reactive_agents` no-ops a repeat call per agent.
Added a dev-time guardrail: `EventStore.emit` warns once per process per
call-site when an `AGENT_RELEVANT_EVENTS` type has zero subscribers.

Migrated sites: `_emit_fin` + all four `emit_refill_events(...)` call sites
+ both custodial-disburse endpoints (`amy/saas/routers/finance.py`),
`JobCtx.events()` (`amy/automation/executors.py`), `_events_with_agents`
(`amy/saas/routers/geo.py`), `refresh_for_user` (`amy/learning_feed/
sensor.py`), `track_progress` (`amy/saas/routers/learning_feed.py`), the
custodial-refill branch of the Gmail auto-poll loop (`amy/saas/app.py`),
and `_emit_biz` (`amy/saas/routers/business.py` — a real bug found here:
`finance.ledger_entry_posted` went through a bare `EventStore`, so the
compliance agent never reacted to a ledger entry posted via the business
router). Intentionally-bare sites (no agent subscribes to their event
types) got a one-line comment instead: the legacy Operational-Layer GitHub
sensor path (`amy/saas/routers/events.py`, `amy/saas/app.py`'s
`_DedupEvents`), and `CollabMaster`'s `register_default_triggers` path
(`amy/collab/orchestrator.py`).

Also fixed a stale assertion in `tests/test_reactive_agents.py` (expected
agent set predated the `learning` agent — pre-existing failure, confirmed
via `git stash` before touching it).

Tests: `tests/test_events_factory.py` (4 passing) — factory-built store
fires an agent; bare store warns once per call-site and doesn't re-warn on
a repeat from the same site; double `register_reactive_agents` on one
instance fires a non-deduped write-proposing agent (`subscription`) exactly
once (counter + exactly-1-approval-row, not masked by dedup keys); isolated
subprocess import of `amy.events.factory` succeeds. Full suite re-run:
same 22 failed / 7 errors as the pre-change baseline (confirmed via `git
stash`, all pre-existing/unrelated — categorizer/BYOK/career-agent/
finance-import/orchestrator LLM-scripting tests), 536+ passing.

### Part 1 — GitHub + Plane registry tools (DONE)

Read tools (`github_list_prs`/`list_issues`/`pr_details`, `plane_list_tasks`/
`task_details`, `meet_upcoming_meetings`) and external-pinned write tools
(`github_comment`, `plane_create_task`, `plane_update_task`) — all in
`amy/tools/connector_tools.py` — talk to the user's already-registered
GitHub/Plane MCP connectors (Layer 1 `McpConnector` rows, the official
`api.githubcopilot.com/mcp` + `mcp.plane.so` presets already in
`index.html`'s MCP Sources panel) via a new shared helper,
`amy/connectors/mcp_call.py::call_mcp_tool()` — resolve connector → list
its advertised tools → pick the first candidate name it actually has → call
→ log to `connector_calls` (new table, `amy/automation/store.py`, Part 3's
health tab reads it). Real MCP servers for the same capability don't agree
on tool/arg names (same problem `amy/learning_feed/aggregator.py` already
solved for HN/YouTube/etc.), so every capability tries a short candidate
list rather than assuming one name.

`amy/tools/registry.py`'s `register_tool()` gained an `extras` dict;
`amy/automation/executors.py`'s `_tier_for(risk, external=False)` hard-pins
`external=True` to tier 2 exactly like `destructive` — `AMY_AGENT_WRITE_TIER`
can soften an ordinary internal write but never an external send, since a
GitHub comment or Plane task create is irreversible once delivered. Write
tools follow the existing `add_subscription` convention: the registry
handler delegates to `amy.automation.executors.execute()`, so an approved
action and a direct human-actor call run through the exact same
`github_comment`/`plane_create_task`/`plane_update_task` executors.

Tests: `tests/test_connector_tools.py` (6 passing) — external-pin holds
even with `AMY_AGENT_WRITE_TIER=0`; an ordinary write still honors it
(negative control); human-actor calls execute + log to `connector_calls`;
read tools resolve `owner`/`repo` from the connector's `default_target`;
missing-connector error is clear. All MCP calls mocked.

### Part 2 — Sensors + reactive agents + jobs (DONE)

`amy/connectors/sensors.py`: `GitHubSensor` (→
`github.pr_review_requested`/`pr_status_changed`/`issue_assigned`) and
`PlaneSensor` (→ `plane.task_assigned`/`task_due_soon`/`task_status_changed`),
same `Sensor` base as `GmailSensor`. Diffing uses a new
`connector_sensor_seen` table (`amy/automation/store.py`,
`sensor_seen_state`/`mark_sensor_seen`) — `None` means "never seen" (fires
once), any other value is the last-known state (a `*_status_changed`/
`*_STATUS_CHANGED` event only fires on an actual transition, never on first
sighting). Known limitation, documented in the module: "assigned to
me"/"review requested of me" isn't filtered against the authenticated
identity — any non-empty reviewers/assignees list counts (fine for a
single-user-per-connector deployment).

`amy/agents/reactive.py`: `pr_to_task` (kill switch `AMY_AGENT_PR_TASK`)
proposes a `plane_create_task` (external → always tier 2) on
`github.pr_review_requested` or a changes-requested `pr_status_changed`,
deduped per PR (`pr_task_{repo}_{number}`, blocks pending/executed re-
proposals — the existing `create_approval` dedup semantics). `meeting_prep`
(kill switch `AMY_AGENT_MEETING_PREP`) has NO event subscription — there's
no natural "meeting starting soon" push event — so its registration is a
documented no-op and the real logic, `meeting_prep_check()`, is called
directly by a new job. It's read-only/tier-0: gathers keyword-matched Plane
tasks + GitHub PRs for meetings inside the prep window
(`AMY_MEETING_PREP_WINDOW_MIN`, default 60 min) and writes one idempotent
vault note per meeting id (dedup on `eid`).

Jobs (`amy/automation/jobs.py`): `meeting_prep_scan` (every 15 min) drives
`meeting_prep_check`. "project_pulse" is NOT a competing briefing — per the
brief, it's `amy/automation/closers.py::_work_section()`, a provider
function `morning_briefing()` calls directly (PRs awaiting review, Plane
tasks due within 48h, today's meetings) — every piece independently
best-effort so a missing connector just omits that piece.

Tests: `tests/test_connector_sensors_agents.py` (5 passing) — sensor diff
cycle (first poll emits, identical second poll emits nothing); PR
status-changed only fires on an actual transition, not first sighting; same
PR event fired twice produces exactly one `plane_create_task` approval row
(dedup, not a double-fire); kill switch suppresses the agent; meeting_prep
writes a vault note + `agent.insight` and stays idempotent across repeated
calls. All MCP/Google Calendar calls mocked.

Also had to re-fix `tests/test_reactive_agents.py`'s registered-agent-set
assertion a second time (grew by `pr_task`/`meeting_prep`, both default-on)
— worth noting as a pattern: this assertion will need updating again
whenever a new default-on agent is added; a set-based `>=` check or an
explicit "these + at least" comment might be worth it if this recurs again.

### Part 3 — /api/connectors/status + Connectors tab (DONE)

`GET /api/connectors/status` (`amy/saas/routers/connectors.py`) unifies
health for Google services (Gmail/Calendar-Meet/Sheets — connected +
scopes_ok from the OAuth token), local MCP servers (jobspy/HackerNews/
YouTube/Dev.to — supervisor process+port state imported *lazily* from
`amy/saas/app.py` inside the endpoint to avoid a circular import, since
routers are imported before `_local_mcp_procs`/`_LOCAL_MCP_SERVERS` are
defined in `app.py`; YouTube's missing `YOUTUBE_API_KEY` surfaces as a
`config_warning`), and external MCP connectors (GitHub/Plane/anything else
registered — tool names/risk from the **local** `amy.tools` registry, not a
live remote call). All health data comes from the `connector_calls` ledger
— the endpoint itself never makes a live call.

Found and fixed a real gap left by Part 2: the sensors had no job driving
`.poll()` periodically (Part 2's spec only listed `meeting_prep_scan`).
Added `connector_sensor_scan` (interval via
`AMY_CONNECTOR_SENSOR_INTERVAL_HOURS`, default 30 min) to `DEFAULT_JOBS`,
running both `GitHubSensor` and `PlaneSensor` each tick (independently
try/excepted) — also what the Connectors tab's "Sync now" button triggers
for GitHub/Plane.

Frontend: new `data-tab="connectors"` card grid in `index.html` — status
dot (green/amber/red, computed from `connected`/`supervisor_up`/
`config_warning`/`last_error` vs `last_success` recency), expandable tool
list with risk-colored chips, "Sync now" wired to
`POST /api/automation/jobs/{job}/run` where a job exists. All inline JS
syntax-checked (`node -e "new Function(...)"` over every `<script>` block).

Tests: `tests/test_connectors_status.py` (2 passing, via `TestClient`) —
status shape with nothing registered, and with a seeded healthy GitHub call
+ a seeded failing Plane call (401) in `connector_calls`.

Manually verified live end-to-end (not just mocked tests) via Playwright
against a running `uvicorn` instance: registered real `github`/`plane`
`McpConnector` rows (intentionally-invalid dummy tokens), loaded the
Connectors tab (screenshot confirmed: Gmail/Calendar/Sheets gray-dot
"not connected", the four local servers amber-dot "supervised, up" +
"not registered as an MCP source yet" warning, GitHub green-dot
"last activity 1m ago" after a seeded ledger row), then clicked "Sync now"
on GitHub — it made a REAL network call to `api.githubcopilot.com/mcp`,
got a genuine `HTTP 401 ... check your token/credentials`, and both
GitHub and Plane cards flipped red with that exact error text after the
page re-fetched status. Confirms Parts 1–3 work together end-to-end
against real external MCP servers, not just against mocks.

## CONNECTOR COMPLETION — summary

All four parts DONE. Full test suite after Part 3: 555 passed, 23 failed
(22 pre-existing baseline failures unrelated to this work, confirmed via
`git stash` at each part boundary, plus one known-flaky filesystem-watcher
timing test that passes in isolation). New test files: `test_events_
factory.py` (4), `test_connector_tools.py` (6), `test_connector_sensors_
agents.py` (5), `test_connectors_status.py` (2) — 17 new tests, all
passing, all external calls mocked except the one manual live-server
Playwright verification above.

---

## Phase: CAREER AUTOPILOT

Builds career capability (job discovery, portfolio analysis, application
pipeline) ON the existing goals/tasks (GoalEngine/PlannerAgent), tool
registry + AGENT_GATE (R1/R3), event bus (event factory, quirk 20), and
MemoryWriter/GraphStore journaling — no parallel goal model, no parallel
inbox, no parallel memory. Job discovery is real data only (jobspy MCP,
port 8935); no LLM-fabricated postings.

### Progress

| Part | Description | Status | Commit |
|---|---|---|---|
| 1 | Career data model + Job Search MCP tools | DONE | 1b2f404 |
| 2 | Career goal flow (orchestrator career template) | DONE | 5183bf1 |
| 3 | Portfolio analyst (GitHub ↔ career) | DONE | c4b7054 |
| 4 | Job scout + match scoring | DONE | 5c14c51 |
| 5 | Application pipeline (prepare → approve → send → track) | PLANNED | |
| 6 | Career tab + briefing integration | PLANNED | |

### Pre-flight findings (verified before planning Parts 1-2)

1. **Job Search MCP (port 8935) — actual shape** (`mcp_servers/jobspy_server.py`):
   ONE tool, `search_jobs(search_term, location="", site_names="indeed",
   results_wanted=20, hours_old=72, is_remote=False, country_indeed="USA")
   -> list[dict]`, wrapping `python-jobspy`'s `scrape_jobs()` across
   indeed/linkedin/zip_recruiter/glassdoor/google/bayt/naukri. Each result
   dict already carries title/company/location/job_url/date_posted/
   job_type/is_remote/salary fields/description — there is **no** separate
   "get one job's details by id" remote tool. `country_indeed` must match
   `location`'s country when `site_names` includes indeed or it silently
   returns zero results (no error) — the job_scout sensor must set this
   from the career profile's target location, not leave the jobspy default.
   Consequence for Part 1: `job_search` (registry tool) maps 1:1 to
   `search_jobs` via `call_mcp_tool`; `job_details` is NOT a live MCP call —
   it reads back an already-discovered row from the local `job_postings`
   table (the full posting, description included, was already captured at
   discovery time).
2. **A pre-existing, conflicting career agent is live in production today.**
   `amy/agents/career.py` (`CareerAgent`) + `amy/intelligence/career/
   {discovery,matcher,resume,normalizer,analytics}.py` is a legacy
   "Operational Layer" sub-agent wired into `CollabMaster`
   (`amy/collab/orchestrator.py`), served at `POST /api/collab/ask` /
   `/api/collab/ask/stream` (`amy/saas/routers/collab.py`) — **which is the
   main chat box in `index.html`** (line ~2028), not a dead code path.
   `discovery.discover_jobs()`'s own docstring: *"we leverage the LLM to
   simulate structured job search results"* — it fabricates 3 job postings
   with invented titles/companies/URLs on every "find jobs for X" chat
   message, zero real data, directly violating this phase's "no fake
   data" constraint. It also writes vault notes directly under
   `06_Job_Search/` via `amy.agent_writeback.WriteProposal` — a **third**
   write-proposal mechanism, parallel to both the Approval Inbox
   (AGENT_GATE) and the universal inbox (`external_draft`), that the user
   approves through a different UI path entirely. Left alone, a user typing
   "find jobs" in the main chat box gets confidently fabricated results
   side-by-side with the new, real Job Scout — this needs an explicit
   decision before Part 1 ships (see design questions below).
3. **SMTP is available, self-detecting, already wired for outbound mail.**
   `amy/notifications/email.py`: `smtp_configured()` checks `SMTP_HOST` env;
   `send_email_alert()` no-ops cleanly when unset. `send_hr_email`'s
   executor should call `smtp_configured()` at execution time and either
   send for real or fall back to a copy-ready draft — self-adapting, not a
   hard branch the user needs to pre-decide. `automation/closers.py`
   already uses this exact pattern (`smtp_configured() and ctx.user_email`).
4. **Field-level encryption helper exists and is reusable.**
   `amy/saas/security.py::encrypt_secret`/`decrypt_secret` (Fernet,
   `AMY_ENC_SECRET`, currently used for stored API keys) — `career_profile.
   resume_text` will use the same helper rather than inventing a second one.
5. **Batch approval — confirmed buildable on the existing executor shape,
   no schema change needed.** `submit_action`/`EXECUTORS` (`amy/automation/
   executors.py`) already take one `action_type` + one arbitrary JSON
   `payload` per approval row — a new `plane_batch_create_tasks` executor
   (payload `{tasks: [{title, description}, ...], project_id}`) loops
   `call_mcp_tool` once per task inside a single approval/execute call,
   exactly like `_exec_custodial_disburse` loops per-beneficiary today. One
   open question is UX, not architecture (see design questions below):
   approving the row creates ALL tasks atomically — is partial/per-task
   approval ever needed for the weekly-milestone breakdown?
6. **Goals/tasks schema (reuse, not extend)**: `goals(id, title, domain,
   status, progress, created_at, target_date, finance_meta)`,
   `milestones(id, goal_id, title, done, position)`,
   `tasks(id, goal_id, title, done, created_at, place_tag)` — all in
   collab.db, owned by `PlannerAgent`/`GoalEngine`
   (`amy/collab/planner.py`, `amy/autonomous/goals.py`). A career goal is
   `domain="career"`; `learning_focuses.goal_id` already FKs into this same
   `goals` table (existing Learning Feed integration) — the career plan
   template reuses that link, doesn't add a new one. `finance_meta` is a
   free JSON column on goals already used for savings targets; a
   `career_meta` sibling (target_role, deadline) is the natural place for
   career-goal-specific fields rather than a new table, keeping ONE goal
   row per career objective consistent with every other domain.
7. **Orchestrator's generic plan loop cannot produce a career fan-out
   as-is.** `amy/automation/orchestrator.py::run_goal()` plans a max of
   4 LLM-decided tool-call steps with a 300s wall-clock budget
   (`_PLAN_MAX_STEPS`, `_TIME_BUDGET_S`) — adequate for "cut spending 10%"
   but not for "fan out across learning focuses + weekly Plane milestones +
   portfolio analysis + job scout activation" in one LLM-improvised pass.
   Part 2 adds a **template detection branch**: goals matching a career
   shape (regex/keyword pre-check, e.g. "become a", "career", target-role
   + duration) skip the generic 4-step LLM plan and run a hardcoded
   fan-out sequence instead (skill-gap LLM call → learning_focus create →
   batched milestone/task proposal → portfolio-analysis trigger →
   job-scout activation), still going through `tools.invoke(actor="agent")`
   for every write so AGENT_GATE still gates each one. This is the same
   "detect a known shape, run a template" pattern jurisdiction packs and
   the Learning Feed's focus→goal linkage already use elsewhere in the
   codebase — not a new architectural idiom.

### Part 1 — Career data model + Job Search MCP tools (file map)

- `amy/automation/store.py` — `AutomationStore._init` gains four
  `CREATE TABLE IF NOT EXISTS` blocks (career_profile, job_postings,
  applications, company_intel), same lazy-init idiom as
  `learning_focuses`/`connector_sensor_seen`. CRUD helper methods
  alongside the existing `create_approval`/`log_connector_call` style.
- `amy/tools/career_tools.py` (new, mirrors `connector_tools.py`):
  `job_search` (RISK_READ, wraps `search_jobs` via `call_mcp_tool`,
  `country_indeed` derived from `career_profile`/args), `job_details`
  (RISK_READ, local `job_postings` row lookup — no MCP call, see finding
  1), `portfolio_repo_list`/`portfolio_repo_details` (RISK_READ, reuse
  `github_list_*`-style calls against the existing GitHub connector —
  no new connector registration), `application_log` (RISK_WRITE, internal
  — status-ladder writes to `applications`), `send_hr_email` (RISK_WRITE,
  `extras={"external": True}` — hard tier-2 exactly like `github_comment`),
  `career_status` (RISK_READ — goal/plan progress + funnel counts for the
  assistant and briefing).
- `amy/automation/executors.py` — `send_hr_email` executor (SMTP-or-draft,
  finding 3), `application_log` executor (or direct DB write if RISK_WRITE
  internal writes can bypass the executor indirection the way `add_task`
  does — TBD at implementation time, matching whichever existing tool it
  resembles more).
- `amy/events/store.py` — new event-type constants
  (`career.goal_set`/`job_discovered`/`application_prepared`/`_sent`/
  `_status_changed`/`portfolio_analyzed`) added to `AGENT_RELEVANT_EVENTS`
  so a bare `EventStore` emitting one warns loudly (quirk 20 guardrail).
- `amy/config.py` — kill switches via the existing `agent_enabled()` helper
  (`AMY_AGENT_CAREER_GOAL`/`_PORTFOLIO`/`_JOB_SCOUT`/
  `_APPLICATION_TRACKER`).
- `tests/test_career_tools.py` (new) — table creation idempotency, each
  tool's happy path against a mocked `call_mcp_tool`/mocked SMTP,
  `send_hr_email` external-pin holds under `AMY_AGENT_WRITE_TIER=0`
  (negative control, same test shape as `test_connector_tools.py`).

### Part 2 — Career goal flow (file map)

- `amy/automation/orchestrator.py` — `_is_career_goal(text) -> bool`
  detector + `_run_career_template(ctx, goal, run_id)` fan-out function,
  called from `run_goal()` before the generic plan branch (finding 7).
  Reuses `_store_plan_graph`/`_mark_task`/`_persist_run`/journaling as-is
  so career runs show up in `GET /api/agent/goals` identically to any
  other orchestrator run.
- `amy/collab/planner.py` / `amy/autonomous/goals.py` — no schema change
  (finding 6); template calls `GoalEngine.create_goal(title, domain=
  "career", target_date=...)` then sets `career_meta` (new JSON column,
  sibling to `finance_meta`) with `{target_role, weekly_milestones: [...]}`.
- `amy/learning_feed/sensor.py` — template calls `add_focus(collab_conn,
  uid, topic, goal_id=career_goal_id)` per identified skill gap (existing
  function, no changes needed).
- `amy/automation/executors.py` — new `plane_batch_create_tasks` executor
  (finding 5) + matching `amy/tools/connector_tools.py` (or
  `career_tools.py`) registry tool, `extras={"external": True}`.
- `amy/agents/reactive.py` — `career_goal` agent: (a) proposes a career
  goal (tier-2, dedup `career_goal_suggest`) when career signals appear
  with no active career goal; (b) nudges (advisory `agent.insight` only)
  a career goal with zero `career.*`/`agent.goal_planned` progress events
  in `AMY_CAREER_STALL_DAYS` (default 5) — same 3-day-window non-nag idiom
  as `relationship_nudges`.
- `tests/test_career_goal_flow.py` (new) — career-shaped goal triggers the
  template not the generic planner; template fan-out creates exactly one
  goal + linked learning_focuses + one batched Plane approval (not N);
  stall nudge fires once per window, not per tick; non-career goal still
  takes the generic 4-step path (regression guard).

**Result (DONE)**: built as specced above, plus `AMY_AGENT_CAREER_GOAL`
kill switch (falls back to the generic planner when off) and a daily
`career_goal_stall_check` job (no natural push event for "N days of
silence", same structural choice as `meeting_prep_scan`). One line worth
recording for future sessions: the template's own goal/milestone creation
(`GoalEngine.create_goal`/`add_milestone`) runs UNGATED — treated as the
orchestrator's own plan bookkeeping, the same line `_store_plan_graph`
already draws for its GraphStore writes — only the batched Plane task
proposal (an external send) goes through `tools.invoke(actor="agent")` and
gets gated. `career_goal_stall_check`'s "progress" signal is system-wide
(any `career.*` event since goal creation), not tagged per-goal, since
exactly one active career-domain goal is the expected steady state; call
this out if multi-goal career tracking is ever added. Full suite: 582
passed, same 23 pre-existing failures as Part 1's baseline (confirmed via
`git stash`), +15 new tests all passing. Also fixed `tests/
test_reactive_agents.py`'s registered-agent-set assertion again (grew by
`career_goal`) — the same recurring maintenance note CONNECTOR COMPLETION
Part 2 already flagged.

### Part 3 — Portfolio analyst (DONE)

`amy/agents/reactive.py::portfolio_analyze(events, ctx, target_role=None,
goal_id=None)` — not a registry tool (same precedent as `meeting_prep_check`:
no risk-classification ambiguity, called directly). Pulls repos via the
existing `portfolio_repo_list` tool, builds a target-role keyword profile
from REAL postings via `job_search` (never LLM memory, reusing
`orchestrator._extract_keywords`), then a **deterministic, auditable**
three-way classification (`_classify_repos`): SHOWCASE (matched >=2
keywords AND no missing-signal), NEEDS WORK (relevant but missing
description/homepage/topics — the only signals a repo-list call actually
returns; "tests" is never claimed as detected, only suggested), NOT
RELEVANT (archived/fork/zero keyword overlap). Classification itself is
never LLM-decided, only the resume-bullet narrative and gap-project ideas
are (ONE batched LLM call, `sensitive=False` — public repo metadata + role
keywords, no resume text — degrades to a deterministic template on
failure/no-LLM). Gap projects (role keywords no repo evidences) batch into
ONE `plane_batch_create_tasks` approval, same atomic pattern as Part 2's
milestones. Output: a vault note (`09_Memory/Portfolio Review - {date}`,
idempotent per user+day), `career.portfolio_analyzed` event + journal, and
a structured result dict (Part 6's Career tab will render it directly).

Three triggers, as specced: on-demand from the career plan template (Part
2's step 5 now calls the real analysis instead of a bare repo-list "first
look"), a new monthly `portfolio_review` job (skips cleanly if no active
career goal), and — deferred to Part 6 — a manual button/route. New
`AMY_AGENT_PORTFOLIO` kill switch (`_portfolio_agent` is a no-op
subscription registered for kill-switch/visibility consistency only, same
reasoning as `_meeting_prep_agent` — there's no push event for "analyze my
portfolio").

Tests: `tests/test_portfolio_analyst.py` (9 passing, all MCP calls mocked)
— three-way classification incl. archived/fork; no-target-role skip;
full-flow happy path (showcase/needs-work/not-relevant counts, vault note,
event); gap projects batch into exactly one tier-2 approval; GitHub
failure degrades to an error dict, never raises; agent registration;
monthly job skips without an active career goal and runs when one exists.
Full suite: 591 passed, same 23 pre-existing failures as Parts 1-2's
baseline, +9 new tests passing. Also updated `_run_career_template`'s
`queued_approvals` counter to fold in `portfolio_analyze`'s own batch
approval (it doesn't surface as a top-level "pending" step result the way
`_log_step`'s detection expects) and `test_reactive_agents.py`'s
registered-agent-set assertion again (grew by `portfolio`).

### Part 4 — Job scout + match scoring (DONE)

`amy/career_scout.py` (new flat module, alongside `amy/patterns.py`/
`amy/financing.py` — not under `amy/connectors/`, since this is career-
domain logic on top of a generic MCP read tool, not a generic connector
capability): `JobScoutSensor` (same `Sensor` base/poll shape as
`GitHubSensor`/`PlaneSensor`) no-ops without an active `domain='career'`
goal, otherwise calls `job_search` for the goal's target_role/location,
dedups new postings via `add_posting_if_new` (Part 1), and — for anything
actually new — runs ONE batched match-scoring LLM call
(`_score_postings`, `sensitive=True`, ranker.py's pattern) before emitting
`career.job_discovered` per posting. Postings at/above
`AMY_CAREER_MATCH_THRESHOLD` (default 70) get a `career_job_match`
notification with the score + shown factors (skill overlap/experience
fit/portfolio evidence/location fit) — labeled an estimate. Scoring
failure degrades to `match_score=NULL` (posting still saved, no
notification) rather than blocking discovery.

Known simplification (documented in the module docstring): the "portfolio
evidence" factor is inferred from `career_profile.skills` only —
`portfolio_analyze`'s SHOWCASE/GAPS classification isn't persisted
anywhere queryable outside its vault note, so there's no richer signal to
feed the scorer yet.

New `job_scout_poll` job (default every `AMY_JOB_SCOUT_INTERVAL_HOURS`=12h,
re-checks the `AMY_AGENT_JOB_SCOUT` kill switch at run time the same way
`learning_feed_refresh` re-checks its own flag, since job rows persist
after the env is turned off). `amy/automation/closers.py::_work_section`
gained `_career_briefing_lines` — high-match jobs discovered in the last
24h, read directly from the already-cached `job_postings` table (no live
MCP call from the briefing itself), independently best-effort like every
other Work-section piece.

Tests: `tests/test_job_scout.py` (8 passing) — no-op without an active
career goal; discover + dedup across two poll cycles; scoring + threshold
notification; LLM-unavailable degrades to unscored (never blocks
discovery); kill switch; job wiring; briefing-line inclusion above/below
threshold. All MCP/LLM calls mocked — tests explicitly force `_get_llm`
to return `None` rather than leaving `ctx.llm` unset, since an unset
`ctx.llm` makes `_get_llm` build a REAL `LLMRouter` and attempt real
provider calls (slow, network-dependent); applied the same fix
retroactively to `tests/test_portfolio_analyst.py`'s equivalent gap (cut
that file's runtime from ~14s to ~2s). Full suite: 600 passed, 22 failed
— the same pre-existing baseline minus one known-flaky filesystem-watcher
timing test that happened to pass this run (documented as flaky since
CONNECTOR COMPLETION Part 3).

### Design decisions (resolved before Part 1 started)

(a) **Legacy `CareerAgent`/`discover_jobs` fake-data path (finding 2)**:
disable job discovery only. `amy/intelligence/career/discovery.py::
discover_jobs()` stops fabricating postings (returns `[]` with a note
pointing at the real Job Scout); `CareerAgent`'s matcher/resume/analytics
intents and its vault-note writes are left untouched for now — smallest
change, no regression to existing chat behavior outside job discovery.
(b) **Batch approval UX (finding 5)**: atomic — one approval row lists
every proposed milestone task, approve creates all of them, reject
creates none. Per-task approval can be added later without a schema
change if needed.
(c) No decision needed on Job Search MCP shape or SMTP (findings 1, 3 —
both self-adapting).
