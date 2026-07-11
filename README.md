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
| **Finance CFO** | Import CSV/XLS/PDF statements, Gmail sync, budgets, subscriptions, investments |
| **Budget suggestions** | LLM-backed caps from income + actual spend + location |
| **Subscription detect** | Finds recurring charges in transaction history |
| **Custodial accounts** | Track SBI-style pass-through accounts without polluting your own finances |
| **Business entities** | Register any side business via a form; per-entity Ledger (Accountant/Auditor) + Compliance suggestions — see `BUSINESS.md` |
| **"Can I afford this?"** | Checks a purchase against cashflow, budget headroom, and goals |
| **Vault** | Obsidian-style markdown note journal, auto-written from events |
| **Knowledge graph** | Cross-source typed nodes + edges with timestamps |
| **Event bus** | All write actions emit typed events; Memory Writer journals them automatically |
| **Context module** | Rolling event window surfaced to agents for LLM injection |
| **Gmail sensor** | Poll-based sensor that wraps Gmail sync and emits structured events |
| **Google OAuth** | Gmail, Calendar, Tasks via Google OAuth 2.0 |
| **Agents / Intelligence / Twin** | Additional AI modules (see routers/) |

## Architecture

```
amy/
  saas/
    app.py          FastAPI entry point (all routers)
    routers/        ~15 routers — finance, auth, connectors, vault, knowledge…
    static/
      index.html    Entire frontend (single ~3000-line file)
  finance/          FinanceEngine, categorizer, import parsers, custodial
  events/           EventStore pub/sub + default triggers
  memory/           MemoryWriter — idempotent vault journaling
  knowledge_graph/  GraphStore — typed nodes + edges
  context.py        ContextModule for agent injection
  vault_watcher.py  mtime-based vault change detector
  llm.py            LLMRouter (multi-provider fallback chain)
  config.py         Env var loader
```

Data: `saas_data/` (gitignored) — `amy_saas.db` (users), `index/{uid}/finance.db`, `index/{uid}/connectors/`.

## Docs

| File | Covers |
|---|---|
| `CLAUDE.md` | Architecture, schema, all routes, quirks — read this first when coding |
| `API_ENDPOINTS.md` | Full endpoint reference |

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
