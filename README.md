# Amy — PersonalOS

Self-hosted personal AI operating system. FastAPI backend + single-page frontend. SQLite-per-user, JWT auth, multi-tenant codebase run for one user.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # add NVIDIA_API_KEY, GOOGLE_CLIENT_ID/SECRET
python -m uvicorn amy.saas.app:app --host 0.0.0.0 --port 8849
# → open http://localhost:8849
```

LLM providers degrade gracefully: NVIDIA → OpenAI → Groq → local Ollama → rule-based template.

## Local MCP servers

The Job Search / HackerNews / YouTube / Dev.to / Courses sources in Account → MCP
Sources are served by the self-hosted MCP servers in `mcp_servers/`. The app
auto-starts and supervises them while it runs (`_local_mcp_supervisor_loop`
in `amy/saas/app.py`; opt out with `AMY_LOCAL_MCP_SERVERS=0`), so normally
there is nothing to start by hand — if a connector shows 502, restart the app.

After pulling changes, restart everything with:

```bash
git pull
# kill the app + local MCP servers (PowerShell):
#   Get-NetTCPConnection -LocalPort 8849,8935,8001,8003,8004,8005 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
python -m uvicorn amy.saas.app:app --host 0.0.0.0 --port 8849
```

To run a server standalone (each in its own terminal):

```bash
python mcp_servers/jobspy_server.py       # Job Search → http://localhost:8935/mcp
python mcp_servers/hackernews_server.py   # HackerNews → http://localhost:8001/mcp
python mcp_servers/youtube_server.py      # YouTube    → http://localhost:8003/mcp
python mcp_servers/devto_server.py        # Dev.to     → http://localhost:8004/mcp
python mcp_servers/courses_server.py      # Courses    → http://localhost:8005/mcp
```

## Features

| Module | What it does |
|---|---|
| **Finance CFO** | Import CSV/XLS/PDF statements, Gmail sync, budgets, subscriptions, investments, income sources |
| **Budget suggestions** | LLM-backed caps from income + actual spend + location |
| **Subscription / investment / income detect** | Finds recurring charges, SIP debits, and salary credits in transaction history (account-scoped) |
| **Custodial accounts** | Track SBI-style pass-through accounts without polluting your own finances; Google Sheets export |
| **Business entities** | Register any side business via a form; per-entity Ledger (Accountant/Auditor) + Compliance suggestions — see `BUSINESS.md` |
| **Obligations & jurisdictions** | Zakat/advance-tax/quarterly-estimate presets per country pack (`amy/jurisdictions/`), live nisab/hawl for zakat |
| **Values screening** | Flags transactions against a values profile (interest, sin-category, etc.) |
| **"Can I afford this?"** | Checks a purchase against cashflow, budget headroom, and goals |
| **Career Autopilot** | Job discovery/scoring, portfolio analysis, application pipeline (draft → approval → send), inbound HR-reply detection |
| **Life Autopilot** | Health targets, behavior-pattern inference, auto-tracked habits, weekly wellbeing index, place-triggered nudges — built on the geo/commitments/captures layers |
| **Learning Feed** | Multi-focus topic tracker fanning out to HN/YouTube/arXiv/Reddit/Dev.to/Courses, LLM-ranked |
| **Connectors** | GitHub + Plane via MCP (PR/task sensors, reactive agents), meeting prep, unified connector health tab |
| **Automation layer** | Tiered agent writes (auto / auto+notify / approval-gated), ~25 scheduled jobs, Approval Inbox, drift tracking |
| **Vault** | Obsidian-style markdown note journal, auto-written from events |
| **Knowledge graph** | Cross-source typed nodes + edges with timestamps |
| **Event bus** | All write actions emit typed events; reactive agents + Memory Writer subscribe |
| **Google OAuth** | Gmail, Calendar, Tasks via Google OAuth 2.0 |
| **Digital Twin / Intelligence / Timeline** | Additional AI modules — see `PROJECT_CONTEXT.md` for the full router-by-router breakdown |

## Architecture

```
amy/
  saas/
    app.py          FastAPI entry point (all routers, local-MCP supervisor)
    routers/        ~30 routers — finance, career, life, learning_feed, connectors,
                     automation, business, obligations, values, geo, commitments…
    static/
      index.html    Entire frontend (single ~5000-line file)
  finance/          FinanceEngine, categorizer, import parsers, custodial, detectors
  automation/       Job scheduler, tool-gated executors, orchestrator, career/life jobs
  agents/           Reactive agents (event-subscribed) + persona sub-agents
  career_scout.py / career_apply.py / career_inbound.py   Career Autopilot
  life/             Life Autopilot (targets, inference, habits, wellbeing, opportunity)
  geo/ commitments/ patterns.py   Context layer (places, deadlines, cadences)
  learning_feed/    Multi-focus learning tracker + MCP source aggregation
  connectors/       Generic MCP client + GitHub/Plane sensors
  events/           EventStore pub/sub + reactive-agent wiring (factory.py)
  memory/           MemoryWriter — idempotent vault journaling
  knowledge_graph/  GraphStore — typed nodes + edges
  jurisdictions/    Country packs (JSON) — zakat/tax/deadlines
  llm.py            LLMRouter (multi-provider fallback chain, sensitive-data routing)
  config.py         Env var loader
```

Data: `saas_data/` (gitignored) — `amy_saas.db` (users), `index/{uid}/finance.db`,
`index/{uid}/collab.db` (events/automation/goals/geo/career/life…),
`index/{uid}/connectors/`. Full schema in `PROJECT_CONTEXT.md`.

## Docs

| File | Covers |
|---|---|
| `CLAUDE.md` | Architecture, schema, all routes, quirks — read this first when coding |
| `PROJECT_CONTEXT.md` | Single-file project brain dump — full architecture, every DB table/column, every API route, event/job/agent catalog, known gaps, and future-enhancement ideas. Paste this whole file into a fresh chat to discuss what to build next. |
| `API_ENDPOINTS.md` | Endpoint reference (finance-focused) |
| `BUSINESS.md` | Business-entity module design |
| `docs/AGENT_PLAN.md` | Source of truth for the agentic-finance project phases |
| `docs/LIFE_AUTOPILOT.md` | Life Autopilot binding spec |

## Google OAuth setup

1. Create OAuth 2.0 credentials in [Google Cloud Console](https://console.cloud.google.com)
2. Add authorized redirect URI: `http://localhost:8849/api/connectors/google/callback`
3. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`
4. Connect from the Account tab in the app

## Git / deploy

```bash
# Never commit: .env, .env.personal, saas_data/, *.key, *.pem, google_token.json
git push origin main
```
