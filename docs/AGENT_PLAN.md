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
| R5 | Briefing upgrades (final integration) | DONE | (this commit) |

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
