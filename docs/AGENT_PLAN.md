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
| 3 | `/api/connectors/status` + Connectors tab (index.html) | TODO | |

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
