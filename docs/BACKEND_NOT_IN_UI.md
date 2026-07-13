# Backend built, no UI yet

Working audit (2026-07-14) of features that exist as real, tested backend
code — routes, registry tools, executors — but have **no way to trigger
or view them from the browser**. Compiled by grepping every route path
added by recent phases against `amy/saas/static/index.html` for an actual
`jget`/`jpost`/`api(...)` call. If a route isn't listed here, either it's
wired up or it's an internal-only endpoint never meant for direct UI use
(e.g. OAuth callbacks).

Use this as a punch list: pick an item, and the backend is already done
— building the UI panel is the entire remaining task, no new business
logic required.

## Career Autopilot — Phases A, B, C, D, E, F, Company Discovery

Landed as backend-only in commit `ade26e1` ("Banking Risk Intelligence
series + Career Autopilot Phases A-F + Company Discovery"). None of the
following seven feature areas have any UI — no card, button, or panel
anywhere in `index.html`, despite each having full test coverage and a
working route.

| Feature | Routes | Tools | What it does |
|---|---|---|---|
| **Phase A — Skill Demand** | `GET /api/career/skill-demand?track=&propose=` | `skill_demand_report` | Market-demand report over discovered postings' keywords per active target track; can propose Learning Feed focuses for frequently-missing skills. Has side effects like `/portfolio` (a GET that writes). |
| **Phase B — Career Intelligence Graph** | *(none — tools only)* | `rebuild_career_graph`, `top_skill_gap`, `companies_matching_profile`, `why_rejected` | Skill/company/project/target-role nodes in the shared knowledge graph; skill-gap roadmaps; companies whose postings repeatedly score well; cross-references a rejection against your current skills for a possible explanation. |
| **Phase C — Career Sprint** | `GET /api/career/sprint/current`, `GET /api/career/sprint/history` | `explain_sprint_progress` | Autonomous weekly sprint (Monday plan / Sunday review) — tasks completed vs. planned, progress %, days remaining. |
| **Phase D — Portfolio Builder + Resume Versions** | `GET/POST /api/career/portfolio/items`, `GET/POST /api/career/resume/versions`, `GET /api/career/resume/performance` | `list_portfolio_items`, `list_resume_versions`, `resume_performance` | Persisted GitHub classification (vs. the old on-demand-only `portfolio_analyze`); track-specific tailored resume drafts layered on the master resume (`amy/career_resume.py`), always tier-2 approved; per-version outcome stats (applications/interviews/offers), versions used <3 times marked `insufficient_data` rather than given a misleading rate. **You confirmed you want this one built next.** |
| **Phase E — Opportunity Radar** | `GET /api/career/opportunities?source=` | `list_opportunities`, `explain_opportunity_score` | HN "Who's Hiring" + GitHub org activity + Product Hunt/Reddit hiring signals for companies already matched in the Phase B graph. No LinkedIn signals anywhere, by design. |
| **Phase F — Interview Memory** | `POST /api/career/interviews`, `GET /api/career/interviews/patterns` | `interview_patterns`, `interview_weakness_report` | Manually-logged interview journal (questions, self-assessed outcome, weakness tags); pattern analysis over your own logged interviews — recurring weaknesses, outcome counts per round type. |
| **Company Discovery** | `GET /api/career/companies?city=&confidence=&is_target=`, `PATCH /api/career/companies/{id}/target`, `GET /api/career/companies/{id}/postings` | `list_companies`, `recent_fast_track_postings` | Free-sources-only company discovery (Greenhouse/Lever/Ashby ATS polling, no LinkedIn) with a confidence score; direct ATS postings are the fastest source in the system (no aggregator lag) but don't auto-enter the application pipeline. |

## Banking Risk Intelligence — action endpoints

The **read-only aggregate view** is wired up (Risk & Compliance tab →
`GET /api/risk/dashboard/executive` + `.../explain`). The **write/action**
endpoints underneath it are not — you can see a flagged fraud transaction
or an open AML case in the dashboard, but there's no button to actually
act on one.

| Feature | Route | What it does |
|---|---|---|
| Review a flagged transaction | `POST /api/finance/fraud/transactions/{tid}/review` | Rule-based risk scoring (no LLM); LOW/MEDIUM apply immediately, HIGH/CRITICAL park as a tier-2 approval. |
| List/inspect flagged transactions | `GET /api/finance/fraud/transactions/{tid}`, `GET /api/finance/fraud/flagged` | — |
| Scan an account for AML patterns | `POST /api/finance/aml/accounts/{aid}/scan` | Structuring/layering/cash-spike/circular-transfer typology detectors. |
| List/inspect/escalate AML cases | `GET/PATCH /api/finance/aml/cases`, `POST .../escalate`, `POST .../sar-draft` | Escalation + a draft Suspicious Activity Report — illustrative, never a real regulatory filing. |
| Recompute credit score | `POST /api/credit/recompute` | Internal 300-900 illustrative score from 8 weighted factors (payment history, income stability, cashflow trend, debt, investments, fraud/AML history, business stability). |
| View score / history | `GET /api/credit/score`, `GET /api/credit/history` | — |
| Simulate / apply for a loan | `POST /api/loans/simulate`, `POST /api/loans/apply` | EMI calculators (reducing-balance, flat-rate, compound, Islamic markup), amortization schedule, approval-probability estimate. |
| List/inspect loans | `GET /api/loans`, `GET /api/loans/{id}`, `GET /api/loans/{id}/schedule` | — |

**Everything in this section is explicitly illustrative/simulated** — see
each module's docstring and the Risk & Compliance tab's own disclaimer.
Never real regulatory, bureau, or lending output.

## Not on this list (already wired, or intentionally backend-only)

- **JD Match Advisor** — full UI card on the Career tab (added same
  session as this doc).
- **Career ladder, goal CRUD + AI milestone suggestions, courses source,
  GitHub→vault project sync, career inbound tracking** — all have UI or
  are background jobs whose output surfaces via notifications/vault
  notes (their intended "UI").
- OAuth callback routes, webhook-style endpoints, and job-handler-only
  code paths (e.g. `career_graph_rebuild`, `career_sprint_generate`
  running on a schedule) aren't meant to be clicked directly — the
  **read** routes above (`skill-demand`, `sprint/current`, etc.) are
  what a UI panel would call to show their output.

## Suggested build order

1. **Resume Versions** (Phase D) — highest leverage: you already have a
   master resume and the JD Match Advisor; versions + performance stats
   close the loop. *(next, per your request)*
2. **Fraud/AML/Credit/Loan action buttons** — the dashboard already shows
   the data; this is "add a button that calls the route that already
   exists," the smallest lift on this list.
3. **Company Discovery** — pairs naturally with the existing "Top-matched
   postings" card on the Career tab.
4. **Phase C Sprint + Phase F Interview Memory** — good candidates for a
   combined "This week" widget.
5. **Phase A Skill Demand + Phase B Graph + Phase E Opportunity Radar** —
   more exploratory/analytical, lower urgency.
