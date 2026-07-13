# CLAUDE.md — Amy PersonalOS

Fast-load context. Read before touching code.

## Run

```bash
python -m uvicorn amy.saas.app:app --host 0.0.0.0 --port 8849
# Kill: Get-NetTCPConnection -LocalPort 8849 | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

App startup auto-launches+supervises the local MCP servers behind the
Job Search/HackerNews/YouTube/Dev.to/Courses connectors (`mcp_servers/*.py`,
ports 8935/8001/8003/8004/8005 — see `_local_mcp_supervisor_loop` in `amy/saas/app.py`).
Force-killing the main app (above) does **not** kill these children on
Windows — they keep running independently, which is intentional (the
supervisor detects they're already up on next start and won't duplicate
them). To stop everything including the children:
```bash
# Kill main app + all four local MCP servers:
Get-NetTCPConnection -LocalPort 8849,8935,8001,8003,8004,8005 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
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
  context.py             ContextModule: rolling event window for LLM prompt
                         injection, manually fed by ONE call site
                         (automation/orchestrator.py::_context_block(), goal
                         planning only) — not a general-purpose "context
                         engine" and not pub/sub (its old .attach() bus
                         subscription had zero callers and was removed).
                         Chat context assembly is a separate, federated path:
                         CollabMaster.handle() (collab/orchestrator.py)
                         stitches MemoryRecall.context_block() + FinanceEngine
                         .context_block() + captures.context_block() + a live
                         Plane MCP call directly — GeoStore/patterns.py feed
                         neither path, only reactive agents via events.
  vault_watcher.py       VaultWatcher: mtime-poll, emits vault.note_edited
  finance/
    engine.py            FinanceEngine: SQLite wrapper
    categorizer.py       Rule-based categorizer (instant, no API cost)
    afford.py            "Can I afford this?" logic
    budget_suggest.py    LLM-backed budget cap suggestions (income+spend+location)
    subscription_detect.py  Recurring charge detector (rule pre-filter → LLM confirm)
    investment_detect.py Recurring SIP/broker debit detector (same rule→LLM
                         pattern; cost_basis = sum of contributions seen,
                         current_value defaults to it — no live price feed)
    income_detect.py     Recurring income-credit detector (same rule→LLM
                         pattern; excludes interest/refund/cashback outright)
    fraud_engine.py      FRAUD DETECTION MODULE (Phase 1) — illustrative/
                         simulated rule-based transaction risk scoring, NOT
                         a real banking fraud system (see its module
                         docstring). Six signals (velocity, round-number,
                         spend-spike-vs-own-average, new-beneficiary/first-
                         time-counterparty, dormant-reactivation, atypical-
                         day-of-week — the last is an honest substitute for
                         "time-of-day anomaly": transactions.date has no
                         time component anywhere in this schema, see quirk
                         28); UNAVAILABLE_SIGNALS names device-fingerprint/
                         impossible-travel/login-anomaly/MCC as honestly
                         uncomputed, never faked. `score_transaction()` is
                         pure/read-only; `review_transaction(ctx, tid)`
                         scores then calls `submit_action()` directly with
                         a severity-computed tier (LOW/MEDIUM auto-apply,
                         HIGH/CRITICAL park as a tier-2 approval) — see
                         quirk 28 for why this bypasses AGENT_GATE's static
                         per-tool tiering. Registry tools: `score_fraud_risk`
                         (read), `review_fraud_transaction` (write),
                         `explain_fraud_score` (read, stored score only,
                         never re-scores), `list_flagged_fraud_transactions`
                         (read) — `amy/tools/fraud_tools.py`. Executor:
                         `fraud_review_action` (`amy/automation/executors.py`)
                         persists the score + emits `fraud.detected` (no
                         reactive-agent subscriber yet — Phase 1 scope).
                         Routes: `amy/saas/routers/fraud.py`. No ML model,
                         no payment-rail simulation, no dashboard UI —
                         explicitly out of scope for Phase 1.
    aml_engine.py        AML MONITORING MODULE (Phase 2 of the same
                         illustrative Banking Risk Intelligence series,
                         reuses Phase 1's patterns — read fraud_engine.py
                         first). NOT a real compliance system (see its
                         module docstring); no sanctions/PEP/country-risk
                         data anywhere — UNAVAILABLE_SIGNALS names all
                         four honestly. Four typology detectors:
                         `detect_structuring` (sub-threshold cluster near
                         an illustrative reporting threshold),
                         `detect_layering` (rapid disproportionate in/out),
                         `detect_cash_spike` (ATM/cash-keyword merchant
                         proxy + Phase 1's spend-spike-vs-own-average
                         magnitude check), `detect_circular_transfers`
                         (portfolio-wide — see quirk 29: uses a DEDICATED
                         `aml_graph.db` via `knowledge_graph/store.py`'s
                         `GraphStore` CLASS, NOT the shared `graph.db`,
                         and a custom directed-cycle DFS over
                         `GraphStore.edges()`, NOT `traverse()`/
                         `neighbors()` — both direction-agnostic and would
                         false-positive on any connected component. Scoped
                         to the user's own accounts + custodial
                         beneficiaries only — real cross-institution
                         layering isn't observable from this schema).
                         `scan_account_for_aml()` is pure/read-only
                         (mirrors `fraud_engine.score_transaction`);
                         `investigate_account(ctx, account_id)` is the
                         side-effecting counterpart — opens/reconfirms an
                         `aml_cases` row per triggered typology (deduped
                         on account+typology+overlapping evidence),
                         UNGATED (no `submit_action` — "the case table
                         holds the investigation, the approval table
                         holds the human decision point"). Case lifecycle
                         `open→investigating→escalated/closed`;
                         `escalate_case()`/`generate_sar_draft()` both
                         call `submit_action(ctx, 2, ...)` with a FIXED
                         tier 2 (not severity-computed like Phase 1's
                         `review_transaction` — these are explicit human-
                         requested steps, not automatic detection output).
                         `build_sar_draft_text()` always opens AND closes
                         with a `"DRAFT — NOT A REAL REGULATORY FILING"`
                         header baked into the text itself (survives a
                         screenshot) — only ever called by the
                         `aml_case_sar_draft` executor on approval, never
                         automatically from a scan. Registry tools:
                         `score_aml_typologies` (read), `scan_account_for_
                         aml` (write, but ungated for `actor="human"` —
                         same registry-classification nuance as Phase 1's
                         `review_fraud_transaction`), `escalate_aml_case`
                         (write), `generate_aml_sar_draft` (destructive) —
                         all always tier-2-gated via their fixed
                         `submit_action` calls regardless of registry
                         risk — `explain_aml_alert` (read, stored case
                         only) — `amy/tools/aml_tools.py`. Executors:
                         `aml_case_escalate` / `aml_case_sar_draft`
                         (`amy/automation/executors.py`). Events:
                         `aml.alert` (every trigger) / `aml.case_opened`
                         (new case only) — no reactive-agent subscriber
                         yet. Routes: `amy/saas/routers/aml.py`. No
                         sanctions/PEP integration, no money-mule
                         detection (needs multi-account network data this
                         single-user system doesn't model), no dashboard
                         UI — explicitly out of scope for Phase 2.
    credit_engine.py      AMY CREDIT SCORE MODULE (Phase 3 of the same
                         series — consumes Phase 1's `transactions.
                         fraud_risk_level` and Phase 2's `aml_cases` as
                         inputs, doesn't duplicate their detection). NOT
                         a real bureau product — every generated string
                         must say "Amy Score — an internal signal, not a
                         credit bureau score," never FICO/CIBIL/
                         equivalent. 300-900 range, `WEIGHTS` dict of 8
                         illustrative-weighted factors (`payment_history`
                         20, `income_stability` 15, `cashflow_trend` 15,
                         `debt` 12, `fraud_history` 10, `aml_alerts` 10,
                         `investment_profile` 10, `business_stability` 8)
                         + `overdrafts`/`bureau_score` always weight 0 /
                         `available:false` — see quirk 30 for why
                         `payment_history` is a disclosed PROXY (this
                         schema has no bill/loan payment tracking —
                         `amy/commitments/engine.py` is return-windows/
                         warranties/renewals/documents, not payment
                         obligations) and why `overdrafts` can never be
                         computed (no `accounts.balance` column anywhere).
                         Unavailable factors are excluded from BOTH the
                         weighted sum and its normalization denominator,
                         so a partial-data profile still scores sensibly
                         instead of erroring. `compute_score(fe)` is
                         pure/read-only; `record_score(ctx)` persists a
                         `credit_scores` row + emits `credit.updated` —
                         no `submit_action`/approval anywhere in this
                         module (a derived read-model refresh, not an
                         external/money-moving action, unlike Phase 1/2's
                         detection tools). `improvement_suggestions` are
                         static per-factor templates (no LLM call) drawn
                         ONLY from the lowest-scoring AVAILABLE factors —
                         never suggests fixing something marked
                         `available:false`. Registry tools (all
                         `RISK_READ`, per explicit spec — computing/
                         storing the score needs no approval gate):
                         `compute_credit_score`, `credit_score_history`,
                         `explain_credit_score` (stored row only, never
                         recomputes), `improve_credit_score` (stored
                         suggestions only) — `amy/tools/credit_tools.py`.
                         Job: `credit_score_recompute`, weekly (Monday-
                         only no-op inside a `daily_at` job — same
                         `compute_next_run`-has-no-weekly-type idiom as
                         `life_wellbeing_weekly`). Routes:
                         `amy/saas/routers/credit.py`. No bureau
                         reconciliation, no loan-decisioning logic (that's
                         Phase 5 loan_engine.py below, which consumes
                         this score) — out of scope for Phase 3.
    loan_engine.py        LOAN UNDERWRITING MODULE (Phase 5, final phase
                         of the series) — an illustrative underwriting
                         SIMULATOR, not a real lending decision engine.
                         Consumes Phase 3's `get_latest_credit_score()`
                         and Phase 4's `loan_config` (via
                         `amy.jurisdictions.load_pack`/`loan_config`) as
                         READ-ONLY inputs — never recomputes either.
                         Reuses `amy/finance/afford.py`'s `can_afford()`
                         AS-IS for the affordability piece (the proposed
                         EMI treated as the "spend") — no new
                         affordability calculator. Calculators:
                         `emi_reducing_balance` (real amortization),
                         `emi_flat_rate` (simple interest),
                         `emi_compound` (simplified — total repayment via
                         monthly compounding spread evenly, since true
                         compound amortization isn't a standard consumer-
                         loan shape), `emi_islamic_markup` (ONE shared
                         simplified cost-plus model across Murabaha/
                         Ijara/Musharakah — real contracts differ
                         materially and need Shariah-board review this
                         demo doesn't implement; `qard_hasan` forces
                         profit_rate to 0 as a structurally distinct,
                         honestly-modeled exception, not a
                         simplification). `build_schedule()` generates a
                         REAL declining-balance table for
                         `reducing_balance`, an even-principal-split
                         table otherwise, final installment absorbing
                         rounding drift so balance hits exactly 0. NOT
                         built: floating rate/prepayment/foreclosure/
                         moratorium (future enhancement, per the prompt's
                         explicit permission to skip). `underwrite()` is
                         pure/read-only, building the full decision
                         contract (approval_probability, risk_category,
                         recommended_rate/amount, EMI, DTI,
                         affordability_score, explanation citing the
                         actual credit score + jurisdiction config values
                         used) — every jurisdiction/loan-type limit comes
                         from Phase 4's config, never hardcoded here.
                         `apply_for_loan(ctx, ...)` ALWAYS parks a FIXED
                         tier-2 approval regardless of risk_category (not
                         severity-computed like Phase 1) — "this engine
                         proposes, it doesn't auto-approve." See quirk 32
                         for the rejection-status reconciliation-on-read
                         design (`_reconcile()`) — deliberately NOT a
                         parallel loan-specific reject endpoint. Registry
                         tools: `simulate_loan` (read, dry-run),
                         `apply_for_loan` (write, but ungated for
                         `actor="human"` — the fixed tier-2
                         `submit_action` inside it is the real gate),
                         `explain_loan_rejection`/`simulate_refinancing`/
                         `compare_loan_offers`/`explain_emi` (all read,
                         stored-decision-data only, never re-underwrite)
                         — `amy/tools/loan_tools.py`. Executor:
                         `loan_decision` (`amy/automation/executors.py`,
                         approve-only — generates the schedule). Events:
                         `loan.requested`/`loan.approved` (both real
                         emit sites) / `loan.rejected` (defined for
                         future use, no emit site yet — rejection is
                         handled by reconciliation, not an executor).
                         Routes: `amy/saas/routers/loan.py`. No real
                         underwriting, no full Islamic-contract
                         compliance — explicitly out of scope for Phase
                         5. (The Compliance Dashboard mentioned as
                         out-of-scope for Phase 5 IS built — see Phase 6
                         below, `saas/routers/risk_dashboard.py`.)

  saas/routers/risk_dashboard.py   COMPLIANCE/RISK DASHBOARD (Phase 6,
                         final phase of the Banking Risk Intelligence
                         series) — PURE READ-ONLY aggregation over
                         Phases 1/2/3/5's tables. No `amy/finance/`
                         companion module by design (the prompt:
                         "must not introduce new detection/scoring
                         logic" — counting/grouping existing rows isn't
                         detection worth a dedicated engine file). No
                         materialized cache table either — single-user
                         demo scale, plain Python-side counting over
                         existing list methods (`list_flagged_
                         transactions`, `list_aml_cases`,
                         `list_credit_score_history`/
                         `get_latest_credit_score`, `loan_engine.
                         list_applications`) is trivially fast; don't
                         add one without re-justifying against real
                         row counts first. Four summaries
                         (`_fraud_summary`/`_aml_summary`/
                         `_credit_summary`/`_loan_summary`), each reused
                         by both its own route and `_executive_summary`
                         — no duplicated query logic. `_credit_summary`
                         is explicitly "your score over time," never a
                         population distribution (there's no population
                         in a single-user system — fabricating one
                         would violate this whole series' honesty
                         rules). The explainability endpoint
                         (`GET /api/risk/dashboard/explain?type=&id=`,
                         backed by `_explain()`) dispatches to the ACTUAL
                         registered `explain_fraud_score`/
                         `explain_aml_alert`/`explain_credit_score`/
                         `explain_loan_rejection` tools via
                         `amy.tools.invoke(actor="human")` — never a
                         reimplementation; `type=loan` is only
                         `available:true` for a REJECTED application
                         (that's what the underlying tool does — an
                         honest `available:false` otherwise, not a new
                         "explain any loan decision" tool). Routes:
                         `GET /api/risk/dashboard/{fraud|aml|credit|
                         loans|executive}` + the explain endpoint above.
                         Frontend: `data-tab="risk"` in `index.html`
                         (Money nav sector) — `loadRiskDashboard()`
                         calls the executive endpoint once, reuses the
                         existing `.kpi`/`.fin-kpis` tile pattern (no
                         chart library — none exists anywhere in
                         `index.html`, checked before adding one) +
                         `toggleRiskExplain()` for the inline explain
                         panel. No per-role dashboards, no new SSE
                         layer, no geo/heatmap viz — explicitly out of
                         scope for Phase 6 (and for this project — the
                         Banking Risk Intelligence demo series is now
                         complete: Fraud → AML → Credit Score →
                         Jurisdiction Config → Loan Underwriting →
                         Compliance Dashboard).

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
                         screening/errand/learning/pr_task/meeting_prep — wired
                         onto EventStore at emit points via amy/events/factory.py
                         (register_reactive_agents(events, ctx), idempotent
                         per-instance — see "Event System" below)
  calendars/             Calendar abstraction (R7A-3): gregorian|hijri|fiscal
  jurisdictions/         Packs (R7B): {uae,us,india}.json + loader/versioning
                         + fx_seed.json. New jurisdiction = new JSON only.
                         Each pack also carries an OPTIONAL `loan_config`
                         section (Phase 4, Banking Risk Intelligence prep
                         for Phase 5 Loan Underwriting — config only, no
                         loan math lives here): `loan_limits` per type
                         (personal/home/business/auto/education, each
                         `{amount, currency, basis}` — same shape as the
                         existing `wealth_threshold` fields),
                         `minimum_income` (`annual`|`monthly` basis —
                         differs realistically per jurisdiction),
                         `max_debt_to_income_ratio`, `late_fee_rules`
                         (tagged union: `percentage_of_overdue`+`rate` vs
                         `flat_fee`+`amount` — a real jurisdiction
                         difference, not an invented one),
                         `interest_calculation_defaults`
                         (simple|compound|reducing_balance),
                         `islamic_finance_available` (only `true` for
                         `uae`), and its own `_disclaimer` scoped to "these
                         loan figures are illustrative" (distinct from the
                         pack's existing `disclaimer`, which covers the
                         tax/obligation figures). `loan_config(pack)` /
                         `validate_loan_config(pack)` in
                         `amy/jurisdictions/__init__.py` — optional but
                         validated whenever present (wired into
                         `validate_pack()`), same treatment
                         `obligation_presets` gets. Route: `GET
                         /api/jurisdictions/{pack_id}/loan-config`
                         (`amy/saas/routers/jurisdictions.py`) — see
                         quirk 31 for the `us.json` (not `usa`) naming.
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
  career_scout.py        CAREER AUTOPILOT: JobScoutSensor (Sensor pattern) —
                         no-ops without an active domain='career' goal,
                         else job_search for the goal's role/location,
                         dedups (job_postings), ONE batched match-scoring
                         LLM call (sensitive=True), emits
                         career.job_discovered, notifies + auto-proposes an
                         application (career_apply.prepare_application) at/
                         above AMY_CAREER_MATCH_THRESHOLD (default 70).
                         job_scout_poll job, default every 12h.
  career_apply.py        CAREER AUTOPILOT: application pipeline. prepare_
                         application() — channel recommendation (email/
                         portal/third_party, regex+heuristic, never
                         fabricates a contact), ATS estimate (deterministic
                         keyword coverage, honest None with no resume on
                         file), company intel (generic "web_search" MCP
                         source — this codebase has none built in, so this
                         is an honest stub returning available=False with
                         none registered, never LLM-fabricated), a
                         sensitive=True draft referencing real SHOWCASE
                         repo names — then ONE approval (send_hr_email for
                         email channel, application_log for portal/third-
                         party). The send is ALWAYS gated via
                         tools.invoke(actor="agent") regardless of caller.
                         application_followup_check job (every 2 days):
                         one follow-up email after 10 days' silence, auto-
                         ghosted after another 21. Part 5E adds the
                         duplicate-application guard (same company active
                         or rejected/ghosted < AMY_CAREER_REAPPLY_DAYS=60
                         → blocked; agent path absolute, manual apply
                         route 409s with ?force=true override) + the
                         referral check (knowledge graph + vault mentions
                         as "warm paths" in the approval — own data only).
  career_inbound.py      CAREER AUTOPILOT Part 5D: inbound HR-response
                         detection riding sync_gmail(inbound_hook=) — one
                         extra targeted messages.list in the SAME pass
                         (never a second poll), thread-match via recorded
                         send_hr_email Message-IDs (applications.
                         thread_refs) then sender/domain/company-token,
                         LOCAL-ONLY classification (keyword-ladder
                         fallback; rejection outranks interview), tier-1
                         application_status_update executor + event +
                         vault journal. Interview/offer notifications are
                         the 5A-5C extension points (prep pack/offer
                         analysis NOT built). Never auto-replies. Seen-
                         dedup in thread_refs → sent→response moves
                         exactly once per reply.
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
  knowledge_graph/store.py  GraphStore: typed nodes+edges (note/email/
                         calendar/task/goal/memory), edge UPSERT with
                         timestamps. Distinct from amy/knowledge/'s own
                         relationships.py::RelationshipEngine below — that
                         one is notes-only (wiki-links + keyword overlap),
                         this one is cross-source.
  knowledge/             Vault RAG (embeddings/retrieval) — chunking.py,
                         embeddings.py, retrieval.py, search.py, metadata.py,
                         relationships.py (notes-only relationship graph, see
                         above), confidence.py. Not covered elsewhere in this
                         file; read the module docstrings for detail.
  connectors/
    mcp.py                MCPConnector: generic MCP client (list_tools/call_tool),
                          Layer 1 of the connector architecture — no per-source code
    mcp_call.py            CONNECTOR COMPLETION: call_mcp_tool() — resolve a
                          registered connector by name → pick the first
                          candidate remote tool name it advertises → call →
                          log to connector_calls (collab.db). Shared by
                          tools/connector_tools.py (reads) and
                          automation/executors.py (external writes).
    sensors.py              GitHubSensor/PlaneSensor (CONNECTOR COMPLETION):
                          poll via mcp_call, diff against connector_sensor_seen
                          (collab.db), emit github.*/plane.* events. See
                          "Connectors" section below for the full detail.
  tools/
    connector_tools.py     CONNECTOR COMPLETION: github_list_prs/list_issues/
                          pr_details, plane_list_tasks/task_details,
                          meet_upcoming_meetings (read); github_comment,
                          plane_create_task/update_task (write,
                          extras={"external":True} — hard-pinned tier 2)
    career_tools.py        CAREER AUTOPILOT: job_search/job_details/
                          portfolio_repo_list/_details/career_status
                          (read); set_career_profile/application_log
                          (write); send_hr_email/plane_batch_create_tasks
                          (write, extras={"external":True} — hard-pinned
                          tier 2). See "Career Autopilot" section below.
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
      connectors.py      Google OAuth flow + disconnect + GET /api/connectors/status
                         (CONNECTOR COMPLETION Part 3 — unified connector health)
      learning_feed.py   Focus CRUD, feed list, save/progress — /api/learning-feed/...
      career.py          CAREER AUTOPILOT: profile GET/PUT, postings/applications
                         GET, application PATCH (human-reported outcome, not
                         gated), portfolio GET (the on-demand analysis trigger —
                         has side effects despite being a GET), postings/{id}/
                         apply POST (Part 5's prepare_application).
      [9 others: vault, knowledge, habits, events, memory, twin, intelligence, agents…]
    static/index.html    Entire frontend (~3000 lines, one file) — includes the
                         data-tab="connectors" health dashboard and
                         data-tab="career" tab (goal header, funnel, top-matched
                         postings, on-demand portfolio analysis — NOT the same
                         as the unrelated legacy data-tab="portfolio" tab)
```

## Data Paths

`{uid}` = a row id from `saas_data/amy_saas.db`'s `users` table — look it
up, don't hardcode one (see "What It Is").

| What | Path |
|---|---|
| User DB | `saas_data/amy_saas.db` |
| Finance DB | `saas_data/index/{uid}/finance.db` |
| Collab DB | `saas_data/index/{uid}/collab.db` — events, automation_jobs/runs/approvals, goals/tasks/milestones, learning_focuses/learning_feed_items, geo_places/visits/cells, values screening_flags, prefs, activities, connector_calls, connector_sensor_seen |
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
POST              /api/finance/investments/suggestions       # detect SIP/broker debits from transactions
PATCH/DELETE      /api/finance/investments/{iid}

# Income
POST/GET          /api/finance/income
POST              /api/finance/income/suggestions            # detect recurring salary/retainer credits
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

# Fraud Detection Module (Phase 1) — illustrative/simulated, see amy/finance/fraud_engine.py
POST              /api/finance/fraud/transactions/{tid}/review          # score + route through the tier pipeline
GET               /api/finance/fraud/transactions/{tid}                 # STORED score only, never re-scores
GET               /api/finance/fraud/flagged                            # list transactions with a stored risk_level != LOW

# AML Monitoring Module (Phase 2) — illustrative/simulated, see amy/finance/aml_engine.py
POST              /api/finance/aml/accounts/{aid}/scan                  # detect + open/reconfirm cases, ungated
GET               /api/finance/aml/cases                                # ?status=&account_id=&typology=
GET               /api/finance/aml/cases/{case_id}
PATCH             /api/finance/aml/cases/{case_id}                      # status -> investigating|closed only
POST              /api/finance/aml/cases/{case_id}/escalate             # tier-2 gated (fixed, not severity-computed)
POST              /api/finance/aml/cases/{case_id}/sar-draft            # tier-2 gated; draft/demo header baked into the text

# Amy Credit Score Module (Phase 3) — illustrative internal score, see amy/finance/credit_engine.py
POST              /api/credit/recompute                                 # on-demand compute + persist, ungated
GET               /api/credit/score                                     # latest stored row
GET               /api/credit/history                                   # ?limit= — trend over time

# Loan Underwriting Module (Phase 5) — illustrative simulator, see amy/finance/loan_engine.py
POST              /api/loans/simulate                                   # dry-run, no persistence
POST              /api/loans/apply                                      # creates application + tier-2 approval
GET               /api/loans                                            # ?status=&jurisdiction=&limit= — reconciled
GET               /api/loans/{application_id}                           # reconciled (see quirk 32)
GET               /api/loans/{application_id}/schedule

# Compliance/Risk Dashboard (Phase 6) — read-only aggregation, see amy/saas/routers/risk_dashboard.py
GET               /api/risk/dashboard/fraud
GET               /api/risk/dashboard/aml
GET               /api/risk/dashboard/credit                               # "your score over time" — no population
GET               /api/risk/dashboard/loans
GET               /api/risk/dashboard/executive                            # all four combined + generated_at
GET               /api/risk/dashboard/explain?type=&id=                    # dispatches to the real explain_* tool
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
- Local MCP servers for HackerNews/YouTube/Dev.to/Courses: `mcp_servers/*.py`
  (courses_server.py = free-courses search: freeCodeCamp curriculum JSON +
  Microsoft Learn OFFICIAL catalog API, 24h in-process cache; Google Cloud
  Skills Boost deliberately omitted — no public API, scraping is ToS-risky.
  Skill-gap targeting is free: the career goal creates one learning_focus
  per gap and poll_all fans every focus out to every promoted source. The
  learning agent proposes a high-relevance course (>=8/10) on a goal-linked
  focus as ONE tier-2 add_goal_task per course, dedup course_{focus}_{url-hash})
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

## Connectors

GitHub + Plane integration, meeting prep, and a unified connector health tab
— built on the existing generic MCP connector registration (Layer 1,
`amy/connectors/mcp.py` + `McpConnector` rows in `amy_saas.db`, registered
via Account → MCP Sources with the `github`/`plane` presets pointing at the
official `api.githubcopilot.com/mcp` and `mcp.plane.so` servers).

- `amy/connectors/mcp_call.py::call_mcp_tool(user_id, store, source,
  candidates, args, target_style)` — the one place that resolves a
  connector by name, lists its advertised tools, picks the first candidate
  name it actually has (real MCP servers for the same capability don't
  agree on naming — same problem `learning_feed/aggregator.py` solved for
  HN/YouTube/etc.), calls it, and logs the attempt to `connector_calls`
  (collab.db: connector, tool, ok, ms, error, ts). `extract_list()` pulls a
  `list[dict]` out of the (often differently-shaped) result.
- Registry tools (`amy/tools/connector_tools.py`): `github_list_prs` /
  `github_list_issues` / `github_pr_details`, `plane_list_tasks` /
  `plane_task_details` (read, call through `call_mcp_tool`);
  `meet_upcoming_meetings` (read, Google Calendar directly — not MCP,
  mirrors `agents/calendar.py`'s `_google_calendar_context`);
  `github_comment`, `plane_create_task`, `plane_update_task` (write,
  `extras={"external": True}` — `amy/automation/executors.py`'s
  `_tier_for(risk, external=...)` hard-pins these to tier 2 exactly like
  `destructive`, so `AMY_AGENT_WRITE_TIER` can never auto-execute an
  irreversible external send). Write-tool handlers delegate to
  `automation.executors.execute()`, same convention as `add_subscription` —
  an approved action and a direct human-actor call run through the same
  `github_comment`/`plane_create_task`/`plane_update_task` executors.
- Sensors (`amy/connectors/sensors.py`, same `Sensor` base as
  `GmailSensor`): `GitHubSensor` → `github.pr_review_requested` /
  `pr_status_changed` / `issue_assigned`; `PlaneSensor` → `plane.
  task_assigned` / `task_due_soon` / `task_status_changed`. Diffed against
  `connector_sensor_seen` (collab.db: sensor, item_key, state, ts) —
  `sensor_seen_state()` returns `None` for "never seen" (fires once) vs.
  any other value for "last known state" (a `*_status_changed` event only
  fires on an actual transition, never on first sighting). **Known
  limitation**: "assigned to me"/"review requested of me" isn't filtered
  against the authenticated identity — any non-empty reviewers/assignees
  list counts (fine for a single-user-per-connector deployment; revisit if
  a connector is ever shared). Driven by the `connector_sensor_scan` job.
- Reactive agents (`amy/agents/reactive.py`): `pr_to_task`
  (`AMY_AGENT_PR_TASK`) proposes a `plane_create_task` (external → always
  tier 2) when a PR needs review or goes to changes-requested, deduped per
  PR (`pr_task_{repo}_{number}`). `meeting_prep` (`AMY_AGENT_MEETING_PREP`)
  is registered but subscribes to nothing — there's no natural "meeting
  starting soon" push event — its real logic is
  `meeting_prep_check(events, ctx)`, called directly by the
  `meeting_prep_scan` job (every 15 min): for each Google Calendar meeting
  inside the prep window (`AMY_MEETING_PREP_WINDOW_MIN`, default 60 min),
  keyword-matches its title/attendees against Plane tasks + GitHub PRs,
  writes ONE idempotent vault note per meeting id, emits `agent.insight`.
  Read-only/tier-0 — never a write proposal.
- "project_pulse" is NOT a competing briefing: `amy/automation/
  closers.py::_work_section(ctx)` is a provider function `morning_briefing()`
  calls directly (PRs awaiting review, Plane tasks due within 48h, today's
  meetings) — every piece independently best-effort, a missing connector
  just omits that piece.
- `GET /api/connectors/status` (`amy/saas/routers/connectors.py`) — health
  for every connector: Google services (Gmail/Calendar-Meet/Sheets, from
  the OAuth token + granted scopes), local MCP servers (jobspy/HackerNews/
  YouTube/Dev.to — supervisor process+port state from
  `_local_mcp_supervisor_loop`, imported *lazily* inside the endpoint to
  avoid a circular import with `amy/saas/app.py`; YouTube's missing
  `YOUTUBE_API_KEY` surfaces as a `config_warning`, not an error), and
  external MCP connectors (GitHub/Plane/anything else registered —
  connected + exposed tool names/risk from the **local** `amy.tools`
  registry, never a live remote `list_tools()` call). Health signals come
  from `connector_calls`; the endpoint itself never makes a live call, so
  status checks stay fast. Frontend: `data-tab="connectors"` in
  `index.html` — status dot (green=healthy, amber=config warning/nothing
  synced yet, red=last call failed/unreachable), expandable tool list,
  "Sync now" (`POST /api/automation/jobs/{job}/run`) where a job exists.

```
GET               /api/connectors/status
```

## Operational Layer — migration status

The pre-SaaS "Operational Layer" (OL) was an earlier attempt at unifying
external-system state (entity registry, connector lifecycle/health, sync,
event replay) behind one façade. Most of it has since been superseded by
the connector/sensor/reactive-agent architecture documented above. Status
per piece, so this doesn't have to be re-derived by tracing imports again:

- **Removed** (`amy/operational/layer.py`/`state.py`/`connectors.py`/
  `sync.py`/`replay.py`/`agent.py`/`models.py`/`scheduler.py`, the
  `/api/ops/*` routes in `saas/routers/memory.py`, `app.py`'s
  `run_ops_maintenance` call, and their 5 dedicated test files): the
  `OperationalLayer` façade had a real, passing test suite but was never
  wired to any frontend call site — confirmed via grep of `index.html`
  before deletion. `amy/agents/family.py` (an orphaned duplicate of
  `folders.py`'s `FamilyAgent`, never imported anywhere) was removed for
  the same reason. The `op_entities`/`op_connector_state` table
  definitions remain in `collab/db.py` (idempotent `CREATE TABLE IF NOT
  EXISTS`, harmless if unused) — not worth a migration to drop.
- **Kept — still load-bearing**: `amy/operational/sensors.py` (`Sensor`/
  `SensorRegistry` base classes — GmailSensor, `connectors/sensors.py`'s
  GitHubSensor/PlaneSensor, `career_scout.py`'s JobScoutSensor, and
  `learning_feed/sensor.py`'s LearningFeedSensor all subclass `Sensor`
  from here) and `amy/sensors/github_sensor.py` + `github_service.py` +
  `github_models.py` (the ORIGINAL GitHub sensor — NOT dead: `amy/sensors/
  mcp_sensor.py::_poll_github` still calls it, driven by `app.py`'s live
  `_mcp_poll_loop` (`AMY_MCP_POLL_MINUTES`, default 30) for any promoted
  GitHub MCP connector, firing a real `mcp_activity` notification. Its
  `github.NEW_*` events have zero reactive-agent subscribers — see quirk
  20's event-bus-factory note — which is a real gap (nothing reacts to
  them beyond that one notification) but not the same thing as dead code;
  don't delete it on that basis alone.
- **Also kept, unrelated naming collision**: `amy/agents/folders.py`'s
  persona sub-agents (HomeAgent/ProfileAgent/ProjectsAgent/FamilyAgent/
  FinancesAgent/CareerAgent/ResourcesAgent/JobSearchAgent/KnowledgeAgent/
  CapturesAgent) are NOT OL stubs awaiting real logic — they're the live
  implementation behind `POST /api/ask` (`amy/engine.py::Engine.master`,
  a `MasterAgent` from `amy/agents/master.py`) whenever `AMY_DYNAMIC_AGENTS`
  is unset/false (the default — confirmed neither `.env` nor
  `.env.personal` sets it), which is separate from `pkos.master.MasterAgent`
  (a different class) that backs `POST /api/collab/ask` via `CollabMaster`.
  Two different `CareerAgent` classes exist in this codebase for the same
  reason: `amy/agents/folders.py::CareerAgent` (persona-only, `/api/ask`)
  and `amy/agents/career.py::CareerAgent` (the "Legacy conflict, resolved"
  one below, wired into `CollabMaster`/`/api/collab/ask`) — don't conflate
  them when tracing a career-related chat answer.
- **Not yet audited**: `amy/agents/knowledge.py` — grepped as unreferenced
  by any import during this pass, but wasn't part of the OL removal scope;
  confirm before deleting.

## Career Autopilot

Job discovery, portfolio analysis, and the application pipeline — built on
the existing goals/tasks (GoalEngine/PlannerAgent), tool registry +
AGENT_GATE, event bus, and MemoryWriter/GraphStore. No parallel goal
model, no parallel inbox, no parallel memory. Real data only: no LLM-
fabricated job postings or company intel.

- **Data model** (`AutomationStore`, collab.db): `career_profile` (one row
  per user — `target_role`/`target_location`/`remote_ok`/`deadline`/
  `skills`, `resume_text` Fernet-encrypted like stored API keys),
  `job_postings` (deduped on `uid+url`), `applications` (status ladder:
  prepared→approved→sent→response→interview→offer, or
  rejected/ghosted at any point; JSON `timeline`), `company_intel`
  (per-company cache, 30-day freshness). `goals.career_meta` (JSON,
  sibling to `finance_meta`) carries `{target_role, weeks}` for a
  `domain='career'` goal.
- **Career goal flow**: a career-shaped goal (`become a`/`switch to`/
  `career` + a role word — `amy/automation/orchestrator.py::
  _is_career_goal()`) sent to `POST /api/agent/goal` runs a templated
  fan-out instead of the generic 4-step LLM plan: parse target role/
  deadline → create the goal (ungated — orchestrator's own plan
  bookkeeping, same line `_store_plan_graph` already draws) → skill-gap
  analysis against REAL postings (`job_search`) → linked
  `learning_focuses` → a deterministic weekly milestone breakdown
  proposed as ONE batched `plane_batch_create_tasks` approval (atomic —
  approve creates every task, reject creates none) → a portfolio first
  look. `career_goal` reactive agent proposes a career goal (tier-2, dedup
  `career_goal_suggest`) when a learning focus trends toward a role-shaped
  topic with none active; `career_goal_stall_check` job nudges (advisory
  only, once in a bounded window — same non-nag idiom as
  `relationship_nudges`) a career goal with no `career.*` activity in
  `AMY_CAREER_STALL_DAYS` (default 5) days.
- **Portfolio analyst** (`amy/agents/reactive.py::portfolio_analyze()`,
  called directly like `meeting_prep_check` — not a registry tool): pulls
  repos via `portfolio_repo_list`, builds a target-role keyword profile
  from REAL postings (never LLM memory), then **deterministically**
  classifies into SHOWCASE / NEEDS WORK / NOT RELEVANT (auditable
  factors: keyword overlap + missing description/homepage/topics signals
  — classification is never LLM-decided). ONE `sensitive=False` LLM call
  writes resume-bullet narratives + gap-project ideas (degrades to a
  template). Gap projects batch into one approval. Output: a vault note
  (`09_Memory/Portfolio Review - {date}`), `career.portfolio_analyzed`
  event, three triggers (career plan step, monthly `portfolio_review`
  job, `GET /api/career/portfolio` as the manual button).
- **Job scout + match scoring** (`amy/career_scout.py::JobScoutSensor`,
  same `Sensor`/poll shape as `GitHubSensor`): no-ops without an active
  career goal; queries `job_search` for the goal's role/location, dedups
  new postings, then ONE batched `sensitive=True` match-scoring LLM call
  (factors: skill overlap/experience fit/portfolio evidence/location fit
  — "portfolio evidence" is inferred from `career_profile.skills` only,
  since `portfolio_analyze`'s classification isn't persisted anywhere
  queryable outside its vault note). Postings at/above
  `AMY_CAREER_MATCH_THRESHOLD` (default 70) notify AND auto-propose an
  application (gated by its own `AMY_AGENT_APPLICATION_TRACKER` switch,
  separate from `AMY_AGENT_JOB_SCOUT` which only gates discovery/scoring).
  `job_scout_poll` job, default every `AMY_JOB_SCOUT_INTERVAL_HOURS`=12h.
- **Application pipeline** (`amy/career_apply.py::prepare_application()`):
  channel recommendation (regex email extraction / agency-keyword
  heuristic / portal fallback — never fabricates a contact), ATS estimate
  (deterministic keyword-coverage math, honestly `None` with no resume on
  file), company intel (see below), a `sensitive=True` draft referencing
  real SHOWCASE repo names (a **cheap** one-posting reuse of
  `_classify_repos`, deliberately not a full `portfolio_analyze()` call,
  which would spam its own gap-project approval per application) — then
  ONE approval: `send_hr_email` (email channel) or `application_log`
  (portal/third-party — no scraping/portal automation, so approving just
  marks the prep-pack ready for manual submission). **The send is always
  routed through `tools.invoke(actor="agent")` inside
  `prepare_application` regardless of who called it** — a human-clicked
  "apply" and the job_scout agent's high-score auto-proposal both require
  the same explicit approval; Amy never submits an application on its
  own. Dedup key `apply_{posting_id}`. `application_followup_check` job
  (every 2 days): ONE follow-up email after `_FOLLOWUP_STALE_DAYS`=10 days
  of silence (dedup `followup_{application_id}`, whose existence in the
  `approvals` table doubles as the "already followed up" check), auto-
  `ghosted` after another `_GHOST_DAYS`=21 days (internal inference, not
  gated). Portal/third-party applications (no captured contact) are
  structurally skipped — the human tracks those manually.
- **Company intel — honest stub, not fabricated data**: this codebase has
  no built-in web-search tool (verified before building this — grepped
  for web_search/tavily/serpapi/duckduckgo/bing across `amy/`, nothing).
  `career_apply._company_intel()` tries a GENERIC `"web_search"` MCP
  source through the same `call_mcp_tool` resolve-call-log helper GitHub/
  Plane/jobspy already use — any web-search MCP the user registers under
  a name containing "web_search" (Brave, Tavily, …) just works. With none
  registered it returns `available: False` honestly rather than asking an
  LLM to guess a company's hiring process. Always cached (30-day
  freshness) with a "signals, not facts" disclaimer.
- **Legacy conflict, resolved**: `amy/agents/career.py`'s `CareerAgent`
  (a pre-SaaS "Operational Layer" sub-agent, still wired into
  CollabMaster at `POST /api/collab/ask` — the main chat box) used to
  fabricate job postings via LLM (`amy/intelligence/career/discovery.py`'s
  own docstring: "we leverage the LLM to simulate structured job search
  results"). `discover_jobs()` is now neutered — returns `[]` and the
  chat response points at the real `job_search` tool instead. `CareerAgent`'s
  matcher/resume/analytics intents and its separate `agent_writeback`-based
  vault-note writes are untouched.
- `GET /api/career/portfolio` runs `portfolio_analyze()` LIVE — it IS the
  "manual button" trigger, so it has side effects (may propose a Plane
  approval, always writes a vault note) despite being a GET; idempotent
  per day. `data-tab="career"` in `index.html` is unrelated to the
  pre-existing `data-tab="portfolio"` tab (a different, legacy project-
  portfolio UI) — the career portfolio section lives inside the career
  tab to avoid the name collision.

- **Career ladder (Part 5F)**: `goals.career_meta` carries `target_role`
  (the role being APPLIED for now — drives scouting/ATS/drafts) and an
  optional `north_star_role` (destination — drives learning focuses,
  milestone skill/portfolio phases, portfolio analysis). "become X then Y"
  parses as a ladder (LLM parse, deterministic `then`/`en route to`/
  `toward` split fallback). The scout reads the GOAL's `target_role` first
  — editing the profile's role alone does NOT re-aim scouting; use
  `PATCH /api/career/goal` (the "Save ladder" control in the career tab
  header). On an accepted offer with a north star present, the wind-down
  bundle PROMOTES instead of closing: goal stays active, north star
  becomes `target_role` (mirrored to the profile), postings archived,
  withdrawals re-parked individually.

- **Learning Driven by Jobs (Phase A)**: `job_postings.keywords` used to
  be hardcoded to `[]` in `career_scout.py` — `JobScoutSensor.poll()` now
  populates it via `_extract_posting_keywords()` (deterministic tokenize
  + stopword filter, no LLM dependency — restates `amy/automation/
  orchestrator.py::_extract_keywords()`'s approach locally rather than
  importing that module's private helper, per-POSTING instead of
  cross-posting). `skill_demand_report(ctx, track)` /
  `skill_demand_reports(ctx)` aggregate keyword frequency per active
  track (`career_profile.target_role` split on `,`/`/`/`&`/" and " into
  possibly several tracks — see `_active_tracks()`; the user's own
  example has 4 at once) over the last `SKILL_DEMAND_WINDOW_DAYS`
  (90)/`SKILL_DEMAND_MAX_POSTINGS` (100) postings, checking `in_profile`
  against `career_profile.skills`. Posting→track matching is a
  **heuristic** (`_track_matches_posting()`, word-overlap against the
  track name) — `job_postings` has no stored "which track was this
  searched under" column, so it's applied uniformly to old and new rows
  rather than trusting a partial tag. The track's own words (e.g.
  "Flutter"/"Developer" for that track) are excluded from the counted
  keywords via `_track_all_words()` — every matched posting mentions them
  by construction, so they're never a genuine skill gap; don't reuse
  `_track_words()` (the narrower, matching-only word list) for that
  exclusion, it deliberately keeps generic role suffixes for a different
  reason (see quirk 33). Proposals go through a NEW `create_learning_focus`
  registry tool (`amy/tools/builtin.py`, `RISK_WRITE`, next to
  `create_goal` — none existed before this phase) via `tools.invoke(ctx,
  ..., actor="agent")`, matching `_learning_agent`'s existing goal-proposal
  pattern exactly rather than inventing a new tier rule — tier is
  whatever `AGENT_GATE`/`AMY_AGENT_WRITE_TIER` already says (default 2).
  Capped at `SKILL_DEMAND_MAX_PROPOSALS_PER_RUN` (3) qualifying skills
  (>`SKILL_DEMAND_PROPOSAL_THRESHOLD_PCT`=25%) per report call, and
  deduped against existing same-topic focuses via a **direct SQL query**,
  not `learning_feed.sensor.list_focuses()` — that helper auto-seeds a
  default focus for a zero-row user (see quirk 33), which a dedup check
  must not accidentally trigger. Tool: `skill_demand_report`
  (`amy/tools/career_tools.py`, `RISK_READ` — the report itself is a
  read; the proposal side effect is what's actually gated, same pattern
  as every registry tool with an internally-gated write across this
  codebase). Event: `career.skill_demand_updated` (genuinely new — distinct
  from `learning.feed_refreshed`, which is about feed items being
  fetched, not a demand report being computed).

- **Career Intelligence Graph (Phase B)**: `amy/career_graph.py` (new flat
  module — same one-module-per-distinct-concern precedent as `career_
  apply.py`/`career_scout.py`, kept separate from `career_scout.py`
  rather than added there since this pulls from `job_postings`+
  `applications`+`career_profile`+GitHub portfolio, not just scouting).
  `rebuild_career_graph(ctx)` populates `skill`/`company`/`project`/
  `target_role` nodes and `requires`/`matched_by`/`demonstrates`/
  `applied_to` edges — into the **SHARED** `graph.db`
  (`amy/knowledge_graph/store.py`'s `GraphStore`), node-id-namespaced
  (`skill:`/`company:`/`project:`/`role:`) to coexist with `amy/
  automation/orchestrator.py`'s `agentgoal:`/`agenttask:` plan-graph
  nodes already in that file — deliberately NOT a dedicated file the way
  AML's circular-transfer graph is (see quirk 34 for the full
  shared-vs-dedicated reasoning). **Never calls `GraphStore.reset()`** —
  that would wipe the unrelated orchestrator nodes; `add_node`/
  `add_edge` are idempotent so re-running just refreshes this phase's own
  nodes/edges (known limitation: stale edges are never pruned). Project
  nodes come from a live `portfolio_repo_list` + `agents.reactive.
  _classify_repos()` call each rebuild — confirmed portfolio SHOWCASE/
  NEEDS-WORK classification is NOT persisted anywhere queryable outside
  a vault note (`portfolio_analyze()`/`career_apply.py::
  _showcase_repo_names()` both recompute it fresh every time), so there
  was nothing to read back instead. `matched_by` edge weight is the
  average STORED `job_postings.match_score` (never recomputed —
  `companies_matching_profile()` follows the same rule, and excludes
  companies with zero SCORED postings rather than treating an unscored
  posting as a 0, mirroring `career_scout.py::_score_postings()`'s own
  "missing index = not scored, not scored zero" convention). `top_skill_
  gap(ctx, target_role)` reuses Phase A's `skill_demand_report()` output
  directly (propose=False — a read-only roadmap view) rather than
  recomputing frequency; `ordering_basis` in its output states the
  ordering is demand-frequency-derived, and no salary/compensation
  number appears anywhere in this module. `why_rejected(ctx,
  application_id)` cross-references the linked posting's keywords
  (EXCLUDING the user's active track's own role-shaped words — same
  `_track_all_words()` exclusion Phase A applies, found necessary via a
  failing test: a posting titled "Flutter Developer at Acme" isn't
  "missing" the skill "Developer") against `career_profile.skills` —
  explicitly says "your CURRENT skill profile" (this schema has no
  historical snapshot of skills at application time), returns
  `confidence: "none"` when the signal is too weak to say anything (no
  posting keywords, or the candidate already had every matched keyword),
  and caps confidence at `"low"`/`"moderate"` otherwise — **never
  `"high"`**, since a keyword-gap correlation is never proof of why a
  human rejected an application. **None of these 3 query tools depend on
  the graph being freshly rebuilt** — each queries `job_postings`/
  `applications`/`career_profile` directly, same style as every Phase A
  tool; the graph serves exploration (`/api/kg/*`, already generic
  across node types) and enriches `career_apply.py`'s existing referral
  search, not as a read dependency here. All 4 tools (`rebuild_career_
  graph`, `top_skill_gap`, `companies_matching_profile`, `why_rejected`)
  are `RISK_READ` in `amy/tools/career_tools.py`, per this phase's own
  explicit "no writes to other tables" framing. Job: `career_graph_
  rebuild`, weekly (Monday-only no-op inside a `daily_at` job, same
  idiom as `credit_score_recompute`/`life_wellbeing_weekly`) — no new
  HTTP route added (the tools are reachable via `/api/assistant/chat`;
  deliberately not scope-creeping beyond "graph + roadmap" into a new
  UI-facing endpoint surface).

- **Autonomous Career Sprint (Phase C)**: `amy/career_sprint.py` (new flat
  module, same one-per-concern precedent as Phase A/B). A weekly plan/
  review loop — `career_sprint_generate` (Monday) / `career_sprint_review`
  (Sunday), both `daily_at`-scheduled jobs self-filtering on `date.today().
  weekday()` (no native weekly schedule type exists — same idiom as
  `career_graph_rebuild`). **A sprint goal is `domain='career_sprint'`,
  deliberately NOT `'career'`** — every existing "find the career goal"
  query (`_active_career_goal`, `job_scout_poll`, `portfolio_review`,
  `career_goal_stall_check`) does `WHERE domain='career' AND
  status='active' ORDER BY created_at DESC LIMIT 1` expecting exactly one
  row; a same-domain sprint goal would out-rank the real goal the instant
  it's created (more recent `created_at`) and silently hijack every one of
  those lookups (e.g. `job_scout_poll` would lose the real `target_role`).
  `career_meta={"sprint": true}` is set too (same JSON-sidecar convention
  as `finance_meta`) but is cosmetic — the domain value is what actually
  disambiguates. **Tier 1, via a direct `submit_action(ctx, tier=1, ...)`
  call, not `tools.invoke(actor="agent")`** — bypassing `_tier_for`'s
  env-driven policy (which defaults non-external writes to tier 2)
  exactly the way `amy/life/habits.py::_complete()` hardcodes its own
  tier for `auto_suggest_check` mode; `_run_career_template`'s own goal/
  milestone creation is UNGATED instead, since it's the direct result of
  a user's own `POST /api/agent/goal` request — that precedent doesn't
  apply to a job firing unprompted on a timer, which needs tier 1's
  record+notify step. **`submit_action`'s tier<=1 path executes BEFORE
  its own `create_approval` dedup check runs** (execute-then-audit, not
  check-then-execute) — a second same-week call would silently create a
  second goal even though the resulting approval row gets rejected as a
  duplicate afterward, so `generate_sprint()` does its own pre-check
  (`_sprint_already_generated()`, querying `approvals` directly) before
  calling `submit_action`, same idiom as `_complete()`'s own
  `_habit_done()` pre-check — found via a failing test, not by
  inspection. Skill gaps come from Phase B's `top_skill_gap()` (never
  recomputed); outstanding learning-focus items match EITHER the active
  career goal's `goal_id` OR a topic that case-insensitively matches a
  current top-skill-gap entry — Phase A's ongoing skill-demand-driven
  focus proposals (`_propose_focuses_for_demand`) never set `goal_id`, so
  goal-linkage alone would miss most of Phase A's real output. Application
  target is the user's own trailing `AMY_CAREER_SPRINT_TRAILING_WEEKS`
  (4)-week average, or `null` + an honest `"reason"` with no application
  history — never an arbitrary fixed number. "Skills added this week" in
  the Sunday review is **always** `available:false` — `career_profile.
  skills` is a single mutable row with no historical snapshot anywhere in
  this codebase, and building one just for this diff is out of this
  phase's scope. Interviews-scheduled is derived from `applications.
  timeline` entries (`status=='interview'` within the review window), not
  a new signal. Sunday review writes one idempotent-per-week vault note
  (`MemoryWriter`, same pattern as `life/review.py::generate_month`) —
  read-only otherwise, a report not a proposal, so no gating. Routes:
  `GET /api/career/sprint/current|history`. Tool: `explain_sprint_
  progress` (`RISK_READ`).

- **Portfolio Builder + Resume Version Manager (Phase D)**: two new flat
  modules, `amy/career_portfolio.py` and `amy/career_resume.py` — kept
  separate since they're genuinely different concerns (repo classification
  persistence vs. resume drafting), same one-per-concern precedent as
  every other CAREER AUTOPILOT phase. **Both new generation functions
  (`propose_portfolio_update`, `generate_resume_version`) call
  `submit_action(ctx, tier=2, ...)` DIRECTLY, bypassing the tools
  registry entirely** — `RISK_WRITE` + `actor="human"` would otherwise
  execute immediately (quirk 15), and this phase's own framing is
  stricter than the usual internal-write default ("no auto-publishing,
  no exceptions" — public-facing material). Same fixed-tier-2 pattern as
  AML's `escalate_case`/`generate_sar_draft` and Loan's `apply_for_loan`.
  `portfolio_items` (new table, `collab.db` via `AutomationStore` — NOT
  `finance.db`, everything career-related already lives in `collab.db`)
  persists `_classify_repos()`'s SHOWCASE/NEEDS_WORK/NOT_RELEVANT output,
  previously reaching only a vault note as formatted text (confirmed via
  `amy/career_graph.py`'s own module docstring). `persist_classification()`
  is called from `portfolio_analyze()`
  (`amy/agents/reactive.py`) right after its EXISTING `_classify_repos()`
  call — the same classify pass, not a second one; `portfolio_analyze`'s
  existing vault-note/gap-project/resume-evolution behavior is untouched.
  **GitHub activity trigger is `pushed_at`-cursor based, NOT commit/
  release-based** — `GitHubSensor` (`amy/connectors/sensors.py`) has no
  commit/release polling at all (confirmed reading it in full; only PR
  review-requests/status-changes and issue-assignment). `scan_github_
  activity()` compares each persisted repo's live `pushed_at` against a
  stored cursor via the SAME `sensor_seen_state()`/`mark_sensor_seen()`
  mechanism GitHubSensor already uses (`connector_sensor_seen` table,
  sensor name `"portfolio_repo_activity"`) — the cursor is SEEDED at
  `persist_classification()` time (found via a failing test: without
  seeding, the very first scan after any portfolio analysis would see
  "never seen" for every repo and propose an update for all of them
  immediately, not just on a genuine subsequent change). Match scoring's
  "portfolio evidence" factor (`amy/career_scout.py::_score_postings()`)
  now injects REAL showcase repo/keyword evidence via `_portfolio_
  evidence_line()` when `portfolio_items` exist, replacing the module's
  former "known simplification" (skills-only) — still degrades to
  skills-only with no persisted items. **Resume versions are additive to
  Part 5E's existing master-resume evolution, not a replacement** —
  `_propose_resume_evolution()`/the `resume_update` executor
  (`amy/agents/reactive.py`/`amy/automation/executors.py`) already
  proposes folding portfolio bullets into the ONE master `career_
  profile.resume_text`; `resume_versions` (new table) is a genuinely
  different concept — multiple labeled, TRACK-specific drafts derived
  FROM the master resume + persisted showcase bullets + Phase A's
  `skill_demand_report()` (used only to ORDER emphasis among skills the
  candidate already owns — never to insert an unproven one). The
  course-completion trigger (`scan_course_completions`, only fires when
  a completed `learning_feed_item`'s focus topic matches a CURRENT Phase
  B `top_skill_gap` entry) REUSES the existing `resume_update` executor
  unchanged rather than adding a new one — it proposes one line under a
  "Continuous learning" section of the master resume, same shape as Part
  5E's own proposals. Content is Fernet-encrypted
  (`amy.saas.security`, same convention as `resume_text_enc`) and
  `list_resume_versions()` never decrypts it — metadata only, matching
  `get_career_profile`'s "never return raw resume text over the wire"
  rule. `resume_performance()` marks any version used by fewer than 3
  applications `confidence:"insufficient_data"` instead of a raw rate.
  `applications.resume_version_id` (idempotent `ALTER TABLE`, nullable,
  no join table) attaches via `PATCH /api/career/applications/{id}`'s
  existing body gaining one optional field. Jobs: `portfolio_activity_
  scan` (12h), `course_completion_scan` (6h) — frequent scan-job idiom
  (`meeting_prep_scan`/`interview_debrief_scan`), not the Monday/Sunday-
  only weekly idiom, since GitHub pushes and course completions happen
  any day. Tools: `list_portfolio_items`/`list_resume_versions`/`resume_
  performance` (`RISK_READ`), `propose_portfolio_update`/`generate_
  resume_version` (`RISK_WRITE` — registry classification nuance only,
  the real gate is the internal fixed `submit_action` call above).

- **Opportunity Radar (Phase E)**: `amy/opportunity_radar.py` — hiring-
  signal aggregation, API-backed sources ONLY. **LinkedIn is a hard ban,
  not an `available:false` stub** — no scraping, no session/cookie
  automation, under any framing, because LinkedIn actively enforces
  against it (including individual accounts) and the realistic
  consequence is the user's own account, used for real applications,
  getting banned. LinkedIn signals simply never appear in output; there
  is no code path that even attempts them. Funding/layoff/acquisition/
  engineering-blog signals are the same kind of absence — no clean
  public API exists for them, so they're never in `reasons`, not
  padded in as `available:false` noise on every result. Four real
  sources: **HN "Who's Hiring"** — `mcp_servers/hackernews_server.py`
  gained ONE new tool, `whos_hiring(query, limit)` (still the public,
  keyless Algolia HN Search API — first finds the latest thread authored
  by `whoishiring`, then searches `tags=comment,story_<id>` for its
  hiring comments; `search_stories` alone can't reach comments, only
  `tags=story` hits). Discovered postings reuse the EXISTING `job_
  postings` table (`source="hn_whos_hiring"`) — same shape `JobScoutSensor`
  already uses, deduped via the same `add_posting_if_new` `UNIQUE(uid,
  url)`. **GitHub org activity** — grounded in Phase B's REAL `companies_
  matching_profile()` output (never an arbitrary company list); company
  name → GitHub org slug is a best-effort guess (`_slugify_company`),
  honestly approximate. Stored in a NEW `opportunity_signals` table
  (company-level signals that aren't a specific posting — a different
  shape than `job_postings`, per the phase's own explicit instruction).
  Deduped via the SAME `sensor_seen_state()`/`mark_sensor_seen()` cursor
  idiom Phase D's `scan_github_activity` already uses (sensor name
  `"opportunity_github_activity"`, keyed `company:repo`) — a still-active
  repo isn't re-signaled every poll. **Product Hunt / Reddit** — the
  exact same generic-MCP-source, honest-`available:false`-until-
  registered pattern `career_apply.py::_company_intel()` already
  established for web search: `call_mcp_tool` with tolerant candidate
  tool names, `ConnectorCallError` → `{"available": False}`, no new
  connector-resolution code. **Scoring is deterministic, never an LLM
  guess** (`score_opportunity()`) — `reasons` only ever includes
  `skill_match_Npct` when there ARE extractable keywords (`career_scout.
  py::_extract_posting_keywords`, the SAME extractor `job_postings.
  keywords` already uses) and `portfolio_evidence_N_projects` only when
  `N > 0` real persisted showcase `portfolio_items` (Phase D) overlap
  those keywords — a reason is never emitted with no backing number.
  **Scored ONCE at discovery, persisted immediately, never recomputed on
  read** — `job_postings.match_score`/`match_factors` (existing columns,
  reused) for posting-shaped opportunities, `opportunity_signals.score`/
  `detail` for company-level ones; `list_opportunities()`/`explain_
  opportunity_score()` only ever read what was stored, same "explain
  never re-fabricates" rule every `explain_*` tool in this codebase
  follows. `OpportunityRadarSensor(Sensor)` (`amy/operational/sensors.
  py`'s base, same pattern `JobScoutSensor`/`GitHubSensor` already use —
  no new base invented) copies `JobScoutSensor.poll()`'s no-active-
  career-goal/no-target-role guard verbatim; each source scan is
  independently try/excepted (mirrors `_connector_sensor_scan`'s per-
  sensor isolation). **No `submit_action` anywhere in this phase** —
  discovery is ungated exactly like `JobScoutSensor`'s own `job_postings`
  writes; only actually applying (the EXISTING `career_apply.prepare_
  application`/approval pipeline) is gated, no shortcut here. Job:
  `opportunity_radar_scan`, reusing the EXISTING `job_scout_interval_
  hours` variable (`AMY_JOB_SCOUT_INTERVAL_HOURS`) rather than a new env
  var, per the phase's own explicit instruction. Tools: `list_
  opportunities`/`explain_opportunity_score` (`RISK_READ` — discovery
  only, no write tool this phase). Route: `GET /api/career/opportunities
  ?source=`.

- **Interview Memory (Phase F, final phase)**: `amy/interview_memory.py`
  — a MANUALLY-LOGGED journal with pattern analysis over time, explicitly
  NOT a passive detection system — every field traces to what the user
  reported, whether via the structured route or the LLM-assisted chat
  path (which only reorganizes, never invents a question/weakness the
  user didn't actually describe). **`interview_debrief_check`
  (`agents/reactive.py`, Part 5E) was NOT duplicated** — confirmed
  reading it in full: it already prompts exactly once per career-linked
  calendar event ending (vault-note skeleton + one notification, deduped
  via its existing prefs-table guard). This phase only added one line to
  that notification's body pointing at `log_interview_from_chat`/`POST
  /api/career/interviews` as the structured, queryable destination — its
  own once-per-event dedup and vault-note behavior are untouched, no
  second "an interview just happened" detector was built. **`company` on
  `interview_logs` is DERIVED from the linked `application_id`'s posting
  at write time when one is given, never independently trusted** —
  `applications` itself has no `company` column (always resolved via
  `get_posting()`, same pattern `career_inbound.py` already uses), so a
  caller passing both `application_id` AND a (possibly stale/wrong)
  `company` gets the real linked value, not their guess. `application_id`
  is nullable — an interview not yet tracked as a formal application (a
  referral chat, an early informational round) still gets a company-only
  row from the caller's own text. **Tier 1, via a direct
  `submit_action(ctx, tier=1, ...)` call** — the same established pattern
  `amy/life/habits.py::_complete()` and `amy/career_sprint.py::generate_
  sprint()` already use for "internal, reversible, no-external-system
  action that still needs a record+notify step," not `_tier_for
  ("write")`'s env-driven default of tier 2 — this is the user's own
  self-report, so it's always auto-executed + notified regardless of
  actor or `AMY_AGENT_WRITE_TIER`. New executor: `interview_log_create`.
  **Skill-gap cross-referencing is READ-ONLY** — `interview_patterns()`'s
  `linked_skill_gaps` looks up `f"skill:{tag.lower()}"` in the EXISTING
  shared `graph.db` (`career_graph.py`'s `_graph_path`/`skill:` node
  convention, Phase B) via `GraphStore.get_node()`; a weakness tag that
  doesn't exactly match a real node stays unlinked, never fuzzy-guessed
  or backfilled — no graph write happens anywhere in this module.
  `interview_weakness_report()` reuses `interview_patterns()` verbatim
  (no duplicated aggregation, no drift between the two). `log_interview_
  from_chat()` resolves `application_id` via the SAME `_company_token`
  heuristic (`career_inbound.py`) `interview_debrief_check` already uses
  against the user's own `interview`/`offer`-status applications — no
  match stays honestly unlinked. Tools: `log_interview`/`log_interview_
  from_chat` (`RISK_WRITE` — registry classification nuance only, the
  real gate is the internal fixed `submit_action(tier=1)` call),
  `interview_patterns`/`interview_weakness_report` (`RISK_READ`). Routes:
  `POST /api/career/interviews`, `GET /api/career/interviews/patterns`.
  This is the final phase in the Career Autopilot build order — no
  further phases chain after it.

- **Company Discovery + ATS Fast-Track (extends Phase E)**:
  `amy/company_discovery.py` — FREE SOURCES ONLY, no exceptions. A
  source needing a credit card or a paid plan beyond a trial allowance
  is simply absent from this module's output, never stubbed
  `available:false` (same "absent, not padded" convention Phase E's
  funding/layoff handling already established) — Naukri/NaukriGulf/
  Bayt/GulfTalent/Fantastic.jobs/Crunchbase/LinkedIn-Jobs-in-any-form/
  Clutch/TechBehemoths/GoodFirms/Tracxn/YourStory/MAGNiTT/Cutshort/
  Wellfound/Instahyre are all permanently excluded, documented once in
  the module docstring, an acknowledged coverage gap (weaker Indian/
  Gulf-region coverage, no LinkedIn enrichment) rather than silently
  worked around. **The prompt's claimed "existing `company_intel` stub
  and normalizer in `career_scout.py`" doesn't exist there** — confirmed
  by reading both files: `company_intel` (table + `_company_intel()`
  stub) is `career_apply.py`'s, `career_scout.py` has none. Three real,
  free tiers: **Tier 1** (Greenhouse/Lever/Ashby public JSON feeds,
  fetched via stdlib `urllib.request` directly — same precedent as
  `amy/obligations/zakat.py::_fetch_spot_usd()` for a free/keyless
  official API, not a new MCP server and not `requests` as a new main-
  app dependency); **Tier 2** (Himalayas/TheirStack via the SAME
  generic-MCP-source/honest-`available:false` pattern `career_apply.py::
  _company_intel()`/`opportunity_radar.py`'s Product Hunt/Reddit legs
  already use — registering these connectors is a user action via the
  EXISTING `POST /api/mcp/connectors` route, never something this module
  inserts on the user's behalf; TheirStack degrades to
  `available:False` for the rest of a run on any `ConnectorCallError`,
  never erroring the whole job); **GitHub** (repo search has no
  `location:` qualifier — that's a user/org search qualifier — so
  location-targeted discovery is a bounded two-step: `search_
  repositories` by keyword, then one `search_users` location check per
  candidate org, independently try/excepted per candidate).
  `detect_ats_platform(url)` is a pure regex matcher (Greenhouse/Lever/
  Ashby URL shapes, `None` — never a guess — on no match) called from
  TWO places: directly during discovery, and opportunistically inside
  `amy/automation/store.py::add_posting_if_new()` — the ONE choke point
  every posting-discovery path in this codebase already funnels through
  (`JobScoutSensor`, `opportunity_radar.py`'s HN scan, this module's own
  polls), so hooking it there covers all of them for free without
  touching each call site individually. **Cadence split**: `ats_fast_
  poll` (hourly) is scoped ONLY to `company_intel` rows with
  `ats_platform` set AND `is_target=1` (the user's own curated targets);
  `company_discovery_scan` (weekly, Monday-only `daily_at` self-filter,
  same idiom as `career_graph_rebuild`) refreshes EVERY `ats_platform`-
  known company regardless of `is_target` — broader, slower. Confidence
  scoring (`_persist_hits`): `sources_count >= 2 OR matched_via ==
  "both"` → `"high"`, else `"verify"` — the prompt's exact rule.
  `company_intel`'s real PK is `(uid, company)` — no synthetic `id`, so
  one was added (idempotent `ALTER TABLE` + a one-time `randomblob`
  backfill for pre-existing rows; `upsert_company_intel()` now also
  supplies a fresh id on its INSERT branch only, never touching an
  existing row's id on conflict) for the new `/api/career/companies/{id}`
  routes to address — `(uid, company)` stays the real upsert key
  everywhere, unchanged. LinkedIn slug enrichment is free-search-lookup
  only (`enrich_linkedin_slug`, the exact `_company_intel()` stub
  pattern) — a `linkedin.com/company/` URL in a real web-search result,
  never derived/guessed from the company name. Discovery stays tier-0
  read-only into the pipeline — a fast-tracked posting never auto-enters
  matching/application, same as every other discovery source. Tools:
  `list_companies`/`recent_fast_track_postings` (`RISK_READ`),
  `set_company_target` (`RISK_WRITE`, executes directly — the user's own
  curation over their own local data, no external effect, same as
  `update_career_application`). Routes: `GET /api/career/companies
  ?city=&confidence=&is_target=`, `PATCH /api/career/companies/{id}/
  target`, `GET /api/career/companies/{id}/postings`.

```
GET/PUT           /api/career/profile
PATCH             /api/career/goal                       # ladder roles (Part 5F)
GET               /api/career/postings | applications | portfolio
PATCH             /api/career/applications/{id}          # human-reported outcome, not gated; resume_version_id optional (Phase D)
POST              /api/career/postings/{id}/apply        # 409 + ?force=true on duplicate-company
GET               /api/career/skill-demand?track=&propose=  # Phase A — has side effects like /portfolio
# Phase B (Career Intelligence Graph) has no new HTTP route — 4 tools only, see above
GET               /api/career/sprint/current | history       # Phase C
GET               /api/career/portfolio/items                # Phase D
POST              /api/career/portfolio/items                # Phase D — manual refresh, always proposes
GET               /api/career/resume/versions | performance   # Phase D — metadata only, never decrypted content
GET               /api/career/opportunities?source=           # Phase E
POST              /api/career/resume/versions                 # Phase D — always proposes, never auto-saves
POST              /api/career/interviews                      # Phase F — tier 1, auto-executed + notified
GET               /api/career/interviews/patterns              # Phase F
GET               /api/career/companies?city=&confidence=&is_target=  # Company Discovery
PATCH             /api/career/companies/{id}/target
GET               /api/career/companies/{id}/postings
```

## Life Autopilot

Full binding spec: `docs/LIFE_AUTOPILOT.md`; progress + design findings:
`docs/AGENT_PLAN.md`'s "Phase: LIFE AUTOPILOT". Health targets, behavioral
pattern detection, habit auto-tracking, a wellbeing index, and place-
triggered opportunity nudges — built on `amy/geo/`, `amy/patterns.py`,
`amy/commitments/`, `amy/captures.py`, the tool registry + AGENT_GATE,
event bus, MemoryWriter/GraphStore. Hard rules (enforced in every part):
advisory never diagnostic (no generated text asserts a mental/physical
state), estimates not medical advice (formulas always shown), propose
don't impose (every new habit/goal/target is tier 2 with evidence), own
baselines day-type-matched, never a nag, coordinates/health values never
reach an LLM prompt or event payload, honest NULLs, grace not punishment.

- `amy/life/targets.py` — pure math, no I/O: Mifflin-St Jeor BMR × activity
  multiplier TDEE, age-band sleep, weight-scaled protein/water; every
  function returns `{value, formula, inputs}`.
- `amy/life/bootstrap.py` (L1) — health profile bootstrap. No pre-existing
  "career vault-bootstrap" pattern exists to clone (verified —
  `career_profile` is `PUT`-only, never vault-parsed); built instead from
  `custodial_ai.py::match_beneficiary`'s fuzzy token-matching
  (`find_health_folder`) and its `sensitive=True` LLM-rescue pattern
  (`parse_health_notes`). Missing folder/essentials → durably-deduped
  notification (prefs-table guard, `AMY_LIFE_RESUGGEST_DAYS`) listing
  exactly what's needed, target features dormant. Complete profile → four
  tier-2 `health_target_propose` approvals (calorie/sleep/protein/water),
  each with its formula shown. `append_weight_log` + `check_weight_shift`:
  a >5% weight shift gets its own tier-2 re-proposal with the delta —
  dedup keys are suffixed per re-proposal (a fixed key would permanently
  block re-proposal once the original was approved, since
  `create_approval`'s dedup blocks pending/executed/auto_executed rows).
  `check_vault_reparse`: poll-driven tier-1 re-parse with a diff when the
  health folder's newest `.md` mtime moves (prefs-table marker,
  `health_bootstrap_check` job) — the job-scan idiom (`meeting_prep_scan`),
  not a live `vault.note_edited` subscription, since `app.py`'s
  `VaultWatcher` still runs a bare `EventStore` (`vault.note_edited` is
  not in `AGENT_RELEVANT_EVENTS`).
- `health_profile` table (`collab.db`): `dob_or_age`, `sex`, `height_cm`,
  `weight_kg`, `activity_level`, `weight_log` (JSON), `constraints`
  (Fernet-encrypted, same convention as `career_profile.resume_text_enc`),
  `provenance` (per-field JSON: `vault`|`manual`), `targets` (JSON,
  populated only on approval — propose don't impose).
- `health_targets` registry tool (read, honest `available:False` with no
  profile). `health_bootstrap` no-op reactive agent (job-driven, same
  idiom as `meeting_prep`/`portfolio`) + daily `health_bootstrap_check`
  job. Kill switch `AMY_AGENT_LIFE_HEALTH`; master switch
  `AMY_LIFE_AUTOPILOT` (read via `config._env`, no dedicated config.py
  constant — same pattern as `AMY_LEARNING_FEED_ENABLED`).
- `amy/life/aggregator.py` (L2) — `compute_day(ctx, date)` builds one
  `life_metrics` row from geo visits (office/commute/gym/home-arrival
  durations, real timestamps), transactions (meals_out/late_night_orders/
  cafe_spend, merchant-keyword based — see constraints below), captures +
  `activities` (sleep-window inference input), and calendar (stubbed
  `None` — no past-date-range calendar helper exists yet; deferred until
  L3's meeting-load agent needs it). Day typing computed HERE, consumed by
  every later part: `away` = `AMY_LIFE_TRAVEL_GRACE_DAYS` (2) consecutive
  days with no home signal (tagged `kind='home'` place visit, or the
  `infer_home_cell()` fallback); `silent` = zero signals across every
  source; else `weekday`/`weekend` from the calendar day-of-week. Sleep
  window only fills in when a home-arrival AND a plausible (120-720 min)
  activity-silence gap both exist — NULL otherwise (conservative, per the
  approved design decision). `amy/life/backfill.py`:
  `python -m amy.life.backfill <email> <start> <end>`, looks the user up
  by email (never a hardcoded uid). `life_metrics_daily` job (00:30,
  previous day, idempotent upsert) emits `life.metrics_computed` (counts
  only) — not yet added to `AGENT_RELEVANT_EVENTS` since no reactive
  agent subscribes until L3 lands (mirrors how `career.*` events were
  handled: defined in Part 1, added to the warn-set only once a real
  subscriber exists). `GET /api/life/metrics?from=&to=` (read-only).
  `TimelineEngine` gained a `daily_metrics` source (best-effort, degrades
  silently if `life_metrics` doesn't exist yet on an older `collab.db`).
- `amy/life/habits.py` (L4) — `habit_links` (`collab.db`, bridges to
  `habits.db` by id) map a habit to a signal + mode (`auto_complete` tier
  0 | `auto_suggest_check` tier 1). Auto-completion (`_complete()`) always
  calls `submit_action()` directly, never `tools.invoke(actor="agent")` —
  that's how it gets tier 0/1 instead of AGENT_GATE's forced tier 2
  (quirk 15); the registered `complete_habit_check`/`adjust_habit_target`
  tools exist separately for human/chat use, where gating IS correct.
  Real-time: `on_place_entered`/`on_place_left` wired as the
  `habit_signals` reactive agent (kill switch `AMY_AGENT_LIFE_HABITS`) on
  `context.place_entered`/`context.place_left` (`CONTEXT_PLACE_LEFT` now
  in `AGENT_RELEVANT_EVENTS`). Day-close only:
  `txn_absence`/`txn_presence`/`reading_minutes`/`sleep_window_met` via
  `evaluate_day_close()`, called from `life_metrics_daily` right after
  that day's row computes (absence can't be judged mid-day).
  `streak_with_grace()` is a NEW grace-aware calculation (not a patch to
  `HabitEngine._streak()`, which has zero grace concept and still backs
  the plain UI elsewhere) — skips `life_metrics.grace` days entirely,
  tolerates up to a per-habit `effective_grace_per_week` (stored in
  `prefs`, key `habit_grace_{habit_id}` — deliberately not
  `HabitEngine.frequency`, a free-text label with no enforced semantics
  anywhere) non-grace misses per ISO week. Adaptation (only
  `frequency='daily'` habits): >=3 failing weeks → one easing proposal;
  >=6 effortless weeks → at most ONE level-up proposal ever (fixed dedup
  key); 2 rejected `adjust_habit_target` approvals silence further
  adaptation for that habit (counted from `approvals`, no new table).
  `suggest_link_for_title()` — pure keyword matching for the Add-habit
  flow, never forced. Routes: `POST/GET /api/life/habits/{id}/link[s]`,
  `DELETE /api/life/habit-links/{id}`,
  `GET /api/life/habits/link-suggestions`.
- `amy/life/baselines.py` (L3) — `day_type_baseline(ctx, metric, day_type,
  ...)`, the shared rolling-baseline helper hard rule 4 requires
  (day-type-matched, grace excluded, `AMY_LIFE_BASELINE_WEEKS`); L5's
  wellbeing index reuses it unchanged rather than reimplementing.
- `amy/life/inference.py` (L3) — nine inference agents
  (commute/meals/sleep/activity/reading/meeting_load/admin/seasonal/
  social) sharing ONE `propose()` framework function (dedup via
  `submit_action`'s own `dedup_key`; post-rejection resuggest window via
  an explicit approvals-table check — `create_approval`'s dedup alone
  doesn't cover rejected rows; drift-pruning silence reuses
  `amy/automation/drift.py`'s existing `_signals()` grouped by
  `(action_type, source=f"life_{agent}")` rather than a new pruning
  table). All nine share ONE no-op reactive-agent stub
  (`_life_agent_noop`, registered nine times under nine kill switches —
  `AMY_AGENT_LIFE_{COMMUTE,MEALS,SLEEP,ACTIVITY,READING,MEETING_LOAD,
  ADMIN,SEASONAL,SOCIAL}`) driven by the daily `life_inference_scan` job.
  `propose_habit`/`propose_goal` executors (new — L1 didn't need them)
  let a proposal create a REAL trackable habit/goal, with `propose_habit`
  able to atomically create its `habit_links` row too — the literal
  mechanism connecting L3's pattern detection to L4's auto-completion.
  Known honest gaps: `meeting_count`/`focus_blocks` stay `None` (no
  calendar signal source built yet) so meeting-load's calendar-block
  half is a documented no-op; `seasonal_notes` in the jurisdiction packs
  had zero Python readers before this — this agent is the first consumer.
- `amy/life/opportunity_rules.py` + `amy/life/opportunity.py` (L9) — a
  plain registry (`RULES`, `@rule("name")`, same idiom as executors.py's
  `EXECUTORS`) of 12 `(ctx, place) -> dict|None` place-triggered checks;
  the dispatcher (`dispatch()`, wired via the `life_opportunity` reactive
  agent on `context.place_entered`) iterates the registry generically —
  new rule types never touch it. Four independent anti-nag controls:
  dedup per rule×place×need (`NotificationStore.exists_today`),
  `AMY_LIFE_OPP_MAX_PER_DAY` (a `prefs` counter), grace suppression
  (yesterday's `life_metrics.grace` — today's own day_type isn't known
  until tomorrow's job run), and drift pruning per rule category (two
  dismissals silence a rule permanently — a NEW `prefs`-counter mechanism
  via `POST /api/life/opportunities/{id}/dismiss`, distinct from
  `amy/automation/drift.py`'s approval-rejection signals since L9 fires
  notifications, not approvals). `gym_prompt` is the one rule that's a
  real tier-0 write (one-tap habit check) rather than an advisory
  notification, per the spec's named exception — routed through
  `submit_action` directly like every other auto-completion. Known
  permanent no-ops (never fire, by honest design not bug):
  `person_proximity` (no person↔place association exists anywhere),
  `pharmacy` (refill commitments don't exist until L8 creates them).
  `custodial_bank` reuses `amy/finance/custodial.py::run_validation()`
  directly; `office_gap` uses the real `meet_upcoming_meetings`/
  `plane_list_tasks` tools (unlike L2's still-stubbed calendar columns).
- `amy/life/wellbeing.py` (L5) — `check_week(ctx, week_start=None)`
  defaults to the most recently FULLY completed week (never in-progress).
  Per-component deltas reuse `baselines.day_type_baseline()` computed PER
  day-type within the week then combined by a day-count-weighted average
  of the DELTAS (not raw values) — stays day-type-matched (hard rule 4)
  even though a week blends weekday/weekend days. Majority-grace week
  (<4 non-grace days) → `line_emitted=False` unconditionally (hard rule
  8), regardless of what the components would otherwise show. An adverse
  week (office +60min/day, sleep -30min/day, or zero gym visits vs a
  nonzero baseline) → exactly ONE observation+option line, reusing L3's
  `propose()` framework verbatim for "declining remembered" (same
  dedup/resuggest-window/drift-silence semantics — deliberate reuse, the
  anti-nag needs are identical). No dedicated kill switch (not in the
  spec's enumerated `AMY_AGENT_LIFE_*` list) — gated by
  `AMY_LIFE_AUTOPILOT` only. `life_wellbeing_weekly` job runs
  `daily_at: "07:15"` but no-ops except on Monday (no native weekly
  schedule type in `compute_next_run` — cheap to poll-and-skip rather
  than add one for a single caller). Terminal-advisory: nothing
  downstream keys on `wellbeing_weekly` within this part.
- `amy/life/meal_captures.py` (L8) — a SECOND `sensitive=True`
  classification pass over a capture's already-extracted caption/OCR/tags
  TEXT (never the image — `captures.py`'s vision call at ingest stays
  cloud-based, unchanged, out of scope). Populates `life_metrics.
  meal_captures`/`meal_calorie_est` (NULL when the classifier can't
  estimate), gated by `AMY_AGENT_LIFE_CAPTURE_MEALS` (a real per-capture
  LLM cost, the one L1-L9 kill switch that trades off cost vs coverage).
  `capture_meal` habit_links (a no-op since L4) now actually checks
  `meal_captures >= min_captures`.
- `amy/life/commitments_life.py` (L8) — `pharmacy_refill_check()`
  proposes a `custom`-kind "Refill: {merchant}" commitment from a
  pharmacy-merchant cadence — the exact signal L9's `pharmacy` rule was
  waiting for (verified end-to-end: propose → approve → the L9 rule now
  fires, where it was previously a documented permanent no-op).
  `annual_checkup_check()` proposes one health-checkup commitment per
  calendar year. Both reuse L3's `propose()` framework; new
  `add_commitment` executor is the only new commitment-writing code —
  `CommitmentEngine`'s deadline ladder is untouched.
- `amy/life/health_data.py` (L8) — wearable stub: tries a generic
  `"health_data"` MCP source via `call_mcp_tool`'s tolerant naming (same
  idiom as `career_apply.py`'s company-intel stub), honest
  `available:False` with nothing registered (the universal case — this
  repo has no built-in wearable connector). When available:
  `aggregator._apply_device_sleep()` prefers device sleep data and sets
  the new `life_metrics.sleep_provenance` column (`'inferred'`|`'device'`);
  `steps`/`workouts` are two new `habit_links` signal types.
- `amy/life/review.py` (L6) — `generate_month(ctx, month=None)`: monthly
  vault note (`09_Memory/Life Review - {month}`), idempotent via
  `MemoryWriter.write_atomic`'s eid dedup. Five sections: observed vs
  `baselines.day_type_baseline()`, Suggested/Accepted/Rejected (`approvals
  WHERE source LIKE 'life_%'` in the target month — every L3/L5/L8
  proposal is source-prefixed `life_`, one query covers them all), Pruned
  (L9's `life_opp_dismiss_*` prefs counters ARE the pruning record, no
  new table). Real gap fixed here: `life.pattern_detected` (defined in
  L2, never actually emitted) now fires from `inference.propose()` on
  every successful proposal — L6's timeline/review depend on it.
  `_life_section` (`amy/automation/closers.py`, wired into
  `morning_briefing` as section 5.6): today's auto-checks, longest
  grace-aware streak (>=3 days), ONE most-recent pattern insight,
  commitments due within 3 days (a genuine pre-existing briefing gap —
  no section surfaced `commitments` before this), L8/L9 signals. Timeline
  needed no new source — `TimelineEngine._items()` already reads every
  `events` row generically, so `life.*` events were already appearing;
  `_short()` gained a `"summary"` key check (helps `agent.insight` too).
  `life_review_monthly` job (`monthly_day: 1, at: "06:30"`), no dedicated
  kill switch (not in the spec's enumerated list) — `AMY_LIFE_AUTOPILOT`
  only, same precedent as L5/L8.
- **L7 (UI)** — `GET /api/life/habits-overview` (per-habit `streak_grace`
  + `linked`/`signal_type`/`mode`, one call instead of per-habit fan-out)
  and `GET /api/life/health/targets` (thin HTTP wrapper around the
  `health_targets` registry tool — never reachable over HTTP before).
  `index.html`'s Habits tab: `loadHabits()` prefers `habits-overview`
  (falls back to plain `/api/habits` on failure) and renders a "tracked
  automatically via {signal}" chip + grace-adjusted streak; new Health
  targets card, Wellbeing card (hidden with no data — never a manufactured
  empty section), and Suggested-for-you card (pending `propose_habit`
  approvals, Approve/Reject via the SAME `/api/automation/approvals/{id}/
  {approve|reject}` endpoint the Agent tab already uses). Goals tab gets
  a parallel Suggested-goals card — one `loadLifeSuggested(kind)`
  function parameterized by `kind`, not two near-duplicates. Manually
  verified live via Playwright against a running server (not just mocked
  tests): a linked habit's badge rendered correctly, a live
  `life_inference_scan` run produced real admin-agent + commitments-
  crossover proposals, and clicking Approve in the browser was confirmed
  (via a direct API check) to actually execute — pending count dropped,
  a real goal appeared in `GET /api/goals`.
- **Known constraints discovered during L1/L2 planning** (see
  `docs/AGENT_PLAN.md` for the full finding list): habits live in a
  SEPARATE per-user `habits.db` (`HabitEngine`), not `collab.db` — L4's
  `habit_links` bridges by id across the two files, no FK. `geo_places.kind`
  is free-text, no enum. `geo_cells` has no time-of-day granularity
  (`(cell, day, hits)` only) — home-cell inference cannot restrict to
  night hours as originally envisioned; it falls back to the single
  most-frequented cell overall. `transactions.date` has no time-of-day —
  `late_night_orders` is a merchant-identity proxy (known late-night-
  delivery merchants), not an hour-verified signal.

## Automation Layer

App loop ticks every 60s (`AMY_AUTOMATION_TICK_SECONDS`), runs due jobs per user,
logs every run to `automation_runs`. All automated writes go through
`submit_action(ctx, tier, …)` — **tier 0** auto, **tier 1** auto+notify,
**tier 2** parked in the Approval Inbox until approved. Executors:
`import_statement` · `custodial_disburse` · `add_subscription` · `set_budget` ·
`add_transaction` · `add_place` · `add_task` · `external_draft` (ack-only) ·
`github_comment` · `plane_create_task` · `plane_update_task` (external —
see "Connectors" below) · `application_status_update` (tier-1 backend for
Part 5D inbound detection) · `resume_update` (tier-2, diff in approval) ·
`career_wind_down` (tier-2 bundle; withdrawal emails re-park individually).
Approve/reject decisions are recorded via DecisionEngine.

Default jobs: `gmail_statement_ingest` (6h, hybrid: saved-map/preset/pdfplumber
→ auto-import tier 1; auto-detect/LLM-map/ambiguous → tier 2 approval, map saved
on approve) · `auto_categorize` (12h, learned rules first) · `anomaly_sentinel` ·
`cashflow_alerts` · `morning_briefing` (07:00, email if SMTP set — folds in a
"Work" section, see "Connectors" below) · `custodial_autopilot` (proposes
prefilled cycle as tier 2) · `autopilot` (05:00) ·
`monthly_close` (1st, CFO report + subscription proposals + compliance refresh) ·
`capture_digest` (20:30, photo-memory day-over-day compare, Sunday = weekly
rollup, writes 09_Memory note so chat recalls it next day) · `place_learning`
(21:00, geo_cells×merchant correlation → tier-2 add_place proposals) ·
`commitment_scan` (08:20, return-window/warranty detection + deadline ladder) ·
`pattern_tasks` (06:30, cadence-due merchants → prefilled task proposals) ·
`relationship_nudges` (09:00, broken transfer rhythms → advisory nudge) ·
`preference_drift` (monthly 2nd, decision-history signals) ·
`meeting_prep_scan` (every 15 min, drives the read-only meeting_prep agent) ·
`connector_sensor_scan` (every `AMY_CONNECTOR_SENSOR_INTERVAL_HOURS`, default
30 min — polls GitHubSensor/PlaneSensor) · `job_scout_poll` (default every
`AMY_JOB_SCOUT_INTERVAL_HOURS`=12h, drives JobScoutSensor) ·
`portfolio_review` (monthly 1st, re-analyzes the active career goal's
portfolio) · `career_goal_stall_check` (daily, advisory nudge only) ·
`application_followup_check` (every 2 days, one follow-up + auto-ghosting —
see "Career Autopilot" below) · `interview_debrief_scan` (hourly, prompts
ONCE for a debrief after a career-linked calendar event ends — durable
prefs-table guard, advisory) · `career_retention` (monthly 3rd, archives
90-day-old unapplied postings + compacts their events; applications are
NEVER deleted) · `health_bootstrap_check` (06:05, LIFE AUTOPILOT L1 —
finds/parses the health vault folder, proposes targets, polls for vault
re-parse; re-checks `AMY_LIFE_AUTOPILOT` + `AMY_AGENT_LIFE_HEALTH` at
runtime) · `life_metrics_daily` (00:30, LIFE AUTOPILOT L2 — computes the
previous day's `life_metrics` row, then runs L4's day-close habit-link
evaluation + adaptation checks, idempotent; re-checks `AMY_LIFE_AUTOPILOT`
at runtime) · `life_inference_scan` (10:00, LIFE AUTOPILOT L3 — runs all
nine inference agents' weekly-rollup checks; each independently
re-checks its own kill switch) · `life_wellbeing_weekly` (07:15,
LIFE AUTOPILOT L5 — computes last week's wellbeing_weekly row; no-ops
except on Monday) · `life_review_monthly` (1st, 06:30, LIFE AUTOPILOT
L6 — monthly Life Review vault note, idempotent per month) ·
`credit_score_recompute` (06:50, AMY CREDIT SCORE MODULE Phase 3 —
weekly, Monday-only no-op inside a `daily_at` job same as
`life_wellbeing_weekly`; recomputes and persists the illustrative Amy
Score) · `career_graph_rebuild` (05:40, CAREER AUTOPILOT Phase B —
weekly, same Monday-only idiom; rebuilds the shared Career Intelligence
Graph) · `career_sprint_generate` (07:30, CAREER AUTOPILOT Phase C —
weekly, Monday-only; generates the week's career sprint goal) ·
`career_sprint_review` (20:00, CAREER AUTOPILOT Phase C — weekly,
Sunday-only; writes the sprint review vault note) · `portfolio_activity_
scan` (every 12h, CAREER AUTOPILOT Phase D — pushed_at-cursor GitHub
activity detection, proposes a targeted portfolio_items refresh) ·
`course_completion_scan` (every 6h, CAREER AUTOPILOT Phase D — proposes
a master-resume bullet only for a completed learning item that closes a
current skill gap) · `opportunity_radar_scan` (reuses `job_scout_poll`'s
own interval, default 12h — CAREER AUTOPILOT Phase E — HN "Who's Hiring"
+ GitHub org activity + Product Hunt/Reddit hiring-signal discovery,
scores once at discovery, no LinkedIn in any form) · `ats_fast_poll`
(hourly — Company Discovery extension — direct Greenhouse/Lever/Ashby
polling, scoped to `is_target=1` companies only) · `company_discovery_
scan` (weekly, Monday-only — Company Discovery extension — broader
ATS refresh + free-tier Himalayas/TheirStack + GitHub fan-out).

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
# Build via the factory (see below), not EventStore(cdb) directly, whenever
# the event type may trigger a reactive agent.
from amy.events.factory import get_events
es = get_events(user.id, cdb, index_dir=paths.index_dir(user.id), user_email=user.email)
es.emit("finance.csv_imported", {"bank_name": ..., "imported": n}, source="finance")

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
github.pr_review_requested / pr_status_changed / issue_assigned   # CONNECTOR COMPLETION — MCP-based, not the legacy amy/sensors/ OL github integration
plane.task_assigned / task_due_soon / task_status_changed
career.goal_set / job_discovered / application_prepared / application_sent /
  application_status_changed / portfolio_analyzed   # CAREER AUTOPILOT
career.skill_demand_updated   # Phase A — {track, postings_analyzed, top_skill}; no subscriber yet
career.sprint_generated / sprint_reviewed   # Phase C — no subscriber yet
career.opportunity_detected   # Phase E — {source, company, score}; no subscriber yet
career.interview_logged   # Phase F — {application_id, company, round_type, self_assessed_outcome}; no subscriber yet
career.job_posting_detected_fast   # Company Discovery extension — {posting_id, company, platform}; no subscriber yet
fraud.detected   # Fraud Detection Module Phase 1 — {transaction_id, risk_level,
                 # recommended_action, reason_code_count} only; no subscriber yet
aml.alert / aml.case_opened   # AML Monitoring Module Phase 2 — {case_id, typology,
                 # risk_level[, evidence_count]} only; no subscriber yet
credit.updated   # Amy Credit Score Module Phase 3 — {score, computed_at} only; no subscriber yet
loan.requested / loan.approved   # Loan Underwriting Module Phase 5 — real emit sites.
                 # loan.rejected is defined but has no emit site (see quirk 32)
```

### Event bus factory + reactive-agent wiring (quirk 20)

`amy.events.factory.get_events(user_id, collab_db, index_dir=None,
user_email="", ctx=None)` is now THE way to build an `EventStore` whose
emit should reach reactive agents — it wraps `EventStore(cdb)` +
`register_reactive_agents(es, ctx)` in one call (lazy-importing
`agents.reactive`/`automation.jobs` inside the function body so
`amy/events/store.py` itself stays import-free of agents/automation — no
`events → agents.reactive → tools → automation → events` cycle). Known
sites already migrated: `_emit_fin`/`emit_refill_events` calls/custodial-
disburse endpoints (`finance.py`), `_emit_biz` (`business.py` — this one was
a live bug: `finance.ledger_entry_posted` went through a bare `EventStore`,
so the compliance agent never fired for ledger entries posted via the
business router), `JobCtx.events()` (`automation/executors.py`),
`_events_with_agents` (`geo.py`), the learning-feed router/sensor, and the
custodial-refill branch of the Gmail auto-poll loop (`app.py`).

A **bare** `EventStore(cdb)` is still valid when the event type genuinely
has no agent subscriber (e.g. the legacy Operational Layer's
`amy/sensors/github_sensor.py` events, `CollabMaster`'s
`register_default_triggers` path in `amy/collab/orchestrator.py`,
`digest.generated`, `custodial.disbursed`/`refilled`) — each such site now
has a one-line comment saying why. Building one bare for an
`AGENT_RELEVANT_EVENTS` type (defined in `amy/events/store.py`, next to the
event-type constants) is no longer silent: `EventStore.emit` logs one
WARNING per process per call-site ("...has ZERO subscribers on this
instance...").

Registration is **idempotent per EventStore instance**:
`register_reactive_agents` tracks already-wired agent names on
`events._registered_agent_keys` and no-ops a repeat call for an agent
already present — calling it (or the factory) twice on the same store fires
each agent's handler exactly once per emit, not twice. Don't rely on
approval-side dedup keys to mask a double-registration bug — a non-deduped
agent (e.g. `subscription`, which sets no `agent_dedup_key`) will double-
propose if this guarantee ever regresses; `tests/test_events_factory.py`
pins it down with a call counter + approval-row count, not just dedup.

Kill switches: `AMY_AGENT_BUDGET` / `_SUBSCRIPTION` / `_COMPLIANCE` /
`_SCREENING` / `_OBLIGATION` / `_ERRAND` / `_LEARNING` / `_PR_TASK` /
`_MEETING_PREP` / `_LIFE_HEALTH` / `_LIFE_HABITS` /
`_LIFE_{COMMUTE,MEALS,SLEEP,ACTIVITY,READING,MEETING_LOAD,ADMIN,SEASONAL,
SOCIAL}` / `_LIFE_OPPORTUNITY` / `_LIFE_CAPTURE_MEALS` (LIFE AUTOPILOT
L1/L3/L4/L8/L9, below).

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
20. A new event-emit site that might trigger a reactive agent should use `amy.events.factory.get_events(...)`, not a bare `EventStore(cdb)` — see "Event bus factory + reactive-agent wiring (quirk 20)" above. A bare store emitting an `AGENT_RELEVANT_EVENTS` type now warns loudly (once per process per call-site) instead of silently dropping the reaction — but the warning is dev-time-only (a log line), so still prefer the factory over discovering the gap in production logs.
21. Refresh-by-topic-text on a multi-row feature (learning_focuses) can resurrect a row a user just deleted, if a `BackgroundTasks` refresh was queued before the delete lands and only runs after. Refresh by row id, not by the text a lookup-or-create query matches on, whenever an id is available.
22. `connector_calls`' `connector` column is a literal hardcoded string ("github", "plane", "google_calendar", or a local-server key like "hackernews") chosen at the `call_mcp_tool(...)`/`log_connector_call(...)` call site — it is NOT the user's own `McpConnector.name` (a user could register their GitHub connector as "my github" or "Work GitHub"). `GET /api/connectors/status` and any new connector-health code must roll up by the call-site's literal name, not by `row.name`, or health data silently won't match.
23. GitHub/Plane sensors and tools don't filter "assigned to me"/"review requested of me" against the authenticated identity — any non-empty reviewers/assignees list counts as a hit (see `amy/connectors/sensors.py`'s module docstring). Correct for today's single-user-per-connector deployment; would need a `get_me`-equivalent lookup if a connector is ever shared across users.
24. Two `agent_gate` internals worth remembering when adding a career (or any external-write) tool: (a) an agent-gated approval's `action_type` is always `"tool_call"` — the real tool name lives in `payload["tool"]`, not `action_type`; querying approvals by tool name means filtering `payload.get("tool")==...`, not `action_type==...`. (b) `_get_llm(ctx)` (`amy/agents/reactive.py`) builds a REAL `LLMRouter` and attempts real provider calls whenever `ctx.llm is None` — tests that don't care about LLM output must monkeypatch `amy.agents.reactive._get_llm` to return `None` (or a stub) explicitly rather than leaving `ctx.llm` unset, or they become slow and network-dependent (found while writing CAREER AUTOPILOT's tests; fixed retroactively in Parts 3-6).
25. `FinanceEngine.overview()` reports the month containing the **most recent transaction** (`latest_month_range()`), not the real calendar month — an imported historical statement that ends before "today" would otherwise render the dashboard as all-zero. Response now includes `"period": "YYYY-MM"`; the frontend (`loadFinOverview()` in `index.html`) shows a "Showing {month} — the most recent month with data" banner whenever `period` isn't the current month. Every OTHER caller (`this_month_spend`/`effective_monthly_income`/`balance_estimate`/`budget_status` default params, obligations/zakat, values screening, budget suggestions, closers) still defaults to the real calendar month via `_this_month_range()` — `latest_month_range()` is dashboard-overview-only; pass `date_range=` explicitly if a caller needs the same "most recent data" behavior elsewhere.
26. `subscription_detect.py` / `investment_detect.py` / `income_detect.py` all bucket recurrence candidates by `(account_id, merchant.lower())`, not merchant alone — the same merchant name recurring across two different accounts (household's two cards, two SIP-linked accounts) must not be merged before the cadence/amount checks, or interleaved dates/amounts corrupt the detector and silently drop a real recurring item. Same precedent as `amy/finance/dedup.py`'s account-scoped bucketing.
27. CSV/XLS import has a manual column-mapping fallback UI (`renderColumnMappingUI()` in `index.html`) for when `POST .../preview/csv` returns `needs_mapping: true` (unrecognized headers — auto-detect, saved map, and bank preset all missed). It renders a header/sample-row picker (Date/Description required; Debit+Credit or a single Amount+optional Dr/Cr-type column), then `POST .../column-map` followed by re-running the same preview call, which should now succeed via the saved-map path.
28. Fraud Detection Module (`amy/finance/fraud_engine.py`): (a) `AGENT_GATE` assigns one **static** tier per tool name and only fires for `actor="agent"` calls — it can't vary tier by a specific score's severity, and the module's real callers (the API routes, the assistant tool) are `actor="human"`, which skips `AGENT_GATE` entirely. So HIGH/CRITICAL→tier-2 routing is enforced by `review_transaction()` calling `submit_action()` directly with a severity-computed tier — the same pattern already used in `amy/agents/reactive.py` for `resume_update`/`career_wind_down`. Don't assume registering `review_fraud_transaction` as `RISK_WRITE` is what protects a CRITICAL score from auto-executing; it isn't, by itself. (b) `transactions.date` is a date-only `"YYYY-MM-DD"` string with no time-of-day component across every import path (CSV/PDF/Gmail) — a literal time-of-day fraud signal can't be computed from real data here; `fraud_engine.py` marks it in `UNAVAILABLE_SIGNALS` and uses a day-of-week signal (`atypical_day_of_week`) as the honest substitute instead of fabricating an hour. Any future signal/feature that wants intraday timing needs a real schema change first.
29. AML Monitoring Module (`amy/finance/aml_engine.py`): (a) circular-transfer detection deliberately does NOT write into the shared `index/{uid}/graph.db` — it uses a dedicated `aml_graph.db` via the same `GraphStore` class. `graph.db` backs `/api/graph/viz` and `career_apply.py`'s referral search (an untyped substring scan over every node label, no type filter) — mixing financial account/beneficiary nodes into it would risk them surfacing in an unrelated career-referral chat answer or the general knowledge-graph visualization. Don't "consolidate" the two graphs without re-solving that leak risk first. (b) `GraphStore.neighbors()`/`traverse()` are direction-agnostic (they union `src=?` and `dst=?` — an A→B edge and a B→A edge look identical to them), so `aml_engine.py`'s cycle detection (`_find_directed_cycles`) works off `GraphStore.edges()` directly (which DOES preserve `src`/`dst`) with its own small DFS, never `traverse()`. Reusing `traverse()` for "is this circular" would silently produce false positives for any merely-connected set of accounts. (c) Circular-transfer detection only sees money movement across the user's OWN linked accounts + custodial beneficiaries — bank statements in this schema never carry a counterparty account identifier, so real cross-institution AML layering isn't observable here; this is disclosed in the module docstring, not a bug to "fix" by trying to infer external counterparties from free-text merchant strings alone.
30. Amy Credit Score Module (`amy/finance/credit_engine.py`): (a) `payment_history` is NOT real bill/loan payment history — this schema has no such tracking. `amy/commitments/engine.py`'s `KINDS` are `return_window/warranty/renewal/document/custom` (life-admin deadlines), and `subscriptions.status` is free-text with no "missed payment" semantic anywhere in the codebase. The factor is computed as a disclosed proxy (commitment completion rate + subscription non-active ratio) — don't reinterpret an "expired" commitment as a missed bill payment anywhere else in this codebase either; it means a return window or warranty lapsed. (b) `overdrafts` is hard-coded `available:false` because `accounts` has NO `balance` column anywhere in this schema — `FinanceEngine.balance_estimate()` is a monthly income-minus-spend delta, not a running account balance. For the same reason, "savings" isn't a separate factor from `investment_profile` — a second factor reading the same `investments` table under a different name would be double-counting, not a real second signal. (c) Unavailable factors are excluded from both the numerator AND the normalization denominator when combining the weighted average — a factor being `available:false` must never silently count as a 0, or a user with (say) no business entity would be penalized for something this system simply doesn't know, not something bad.
31. Jurisdiction pack ids don't match ISO/common-name expectations 1:1 — the US pack is `amy/jurisdictions/us.json` with `id: "us"`, NOT `usa.json`/`"usa"` (a wrong guess here 404s `load_pack("usa")` silently via `PackError`, it doesn't fall back). Packs are flat files (`amy/jurisdictions/{id}.json`), never subdirectories, and there is no separate `tax.json` — each pack's tax figures live inline as its own `tax_facts` array. Phase 4's `loan_config` section (see the jurisdictions module-tree entry above) is OPTIONAL per pack — always check `loan_config(pack) is not None` (or catch the route's 404) before assuming a pack has one; don't assume every jurisdiction pack has loan support just because these three demo packs do.
32. Loan Underwriting Module (`amy/finance/loan_engine.py`) rejection is handled by **reconciliation-on-read**, not a dedicated executor or a loan-specific reject endpoint. `executors.reject()` is generic — it only marks the `approvals` row `'rejected'`, with no per-action-type hook to sync `loan_applications.status`. `loan_engine._reconcile()` (called by `get_application()`/`list_applications()`, and therefore by both `GET /api/loans` routes) checks: if a `'pending'` application's linked approval is `'rejected'`/`'expired'`, update and return that status. **Do not add a parallel `POST /api/loans/{id}/reject` endpoint** — a human rejecting through the standard Approval Inbox UI (the real path, same as every other Banking Risk Intelligence phase) would bypass it, leaving `loan_applications.status` silently stuck at `'pending'` forever; that was the exact bug this design avoids. If a future change needs an actual "on-rejection" side effect (e.g. a notification), add it inside `_reconcile()` itself, not a second decision path.
33. `amy.learning_feed.sensor.list_focuses(collab_conn, uid)` is NOT a pure read — for a user with zero `learning_focuses` rows it auto-seeds one from the legacy single-focus resolver (`resolve_focus()`), a real side effect meant for the Learn tab's first-time UX. Any new code that wants to check "does a focus for X already exist" (e.g. `career_scout.py`'s skill-demand dedup, quirk before this one's spirit) must query `learning_focuses` directly (`SELECT topic FROM learning_focuses WHERE uid=? AND active=1`), never via `list_focuses()`, or the check itself will silently create an unwanted default focus. Relatedly, `career_scout.py` keeps TWO different track-word helpers on purpose — `_track_words()` (excludes generic role suffixes like "developer"/"engineer", used for posting↔track matching, since matching on those alone would hit nearly any tech posting) and `_track_all_words()` (keeps every word including those suffixes, used to exclude the track's own name from counted skill-demand keywords, since even "developer" showing up in 100% of a "Flutter Developer" track's postings isn't a skill gap). Don't collapse them into one list — each exists for a specific, different reason.
34. Two career-adjacent graphs exist in this codebase and they are deliberately NOT the same choice of shared-vs-dedicated file. `amy/finance/aml_engine.py`'s circular-transfer graph uses a DEDICATED `aml_graph.db` because financial account/beneficiary nodes in the shared `graph.db` would leak into `career_apply.py`'s referral search (an untyped substring scan over every node label) and the general knowledge-graph viz. `amy/career_graph.py`'s Career Intelligence Graph (Phase B) uses the OPPOSITE choice — the SHARED `graph.db` — because there's no such cross-domain concern: `graph.db` is already career-adjacent (`amy/automation/orchestrator.py`'s `agentgoal:`/`agenttask:` plan-graph nodes), and adding `skill:`/`company:`/`project:`/`role:`-prefixed nodes there directly improves that same referral search. Don't "fix inconsistency" by making these match each other — each is the correct choice for its own domain. Corollary: `career_graph.py::rebuild_career_graph()` must NEVER call `GraphStore.reset()` (unlike `aml_engine.py`, which resets its dedicated graph every run) — that would wipe the unrelated orchestrator.py nodes out of the shared file. `career_graph.py`'s `why_rejected()` independently rediscovered `career_scout.py`'s "exclude the track's own role-shaped words" lesson (quirk 33) — found via a failing test, not by inspection — worth remembering as a general rule any NEW code reading `job_postings.keywords` for a specific posting should apply too, not just the two call sites that currently do.

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
