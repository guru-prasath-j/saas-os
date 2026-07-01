# PersonalOS — Deployable Product Layer

Turns the system into a product: two modes, an agent dashboard, an agent
marketplace, an auto profile builder, knowledge-graph viz, proactive suggestions,
a full REST surface, and CI/deploy. Modular and extensible; offline-testable.

## Requirement coverage

| # | Requirement | Where | Status |
|---|---|---|---|
| 1 | Two modes (private vs public portfolio) | `product/portfolio.py`, `connectors/` gating | ✅ |
| 2 | Agent dashboard (agents, notes, confidence, relationships, memory) | `product/dashboard.py` → `/api/dashboard` | ✅ |
| 3 | Agent marketplace (enable/disable) | `product/marketplace.py` → `/api/agents...` | ✅ |
| 4 | Profile builder (skills/projects/interests/goals) | `product/profile.py` → `/api/profile` | ✅ |
| 5 | Knowledge graph viz API | `product/graphviz.py` → `/api/graph/viz` | ✅ |
| 6 | Proactive suggestions | `product/suggestions.py` → `/api/suggestions` | ✅ |
| 7 | REST APIs (upload/query/dashboard/relationships/profile) | `saas/app.py` | ✅ |
| 8 | Deployment (FastAPI, Docker, GitHub Actions, Railway/Render) | `Dockerfile.saas`, `.github/workflows/ci.yml`, `OPERATIONS.md` | ✅ |
| 8b | Email / Calendar / Tasks access (private mode) | `connectors/` (local provider; Gmail/Google plug in) | ✅ framework + local provider |

## Two modes

- **Private mode** — full access: vault, memory, captures, **email/calendar/tasks**
  (via connectors), finance/family.
- **Public portfolio mode** — `build_portfolio()` exposes ONLY
  projects/skills/learning-roadmap, and **hard-blocks** finance, family, health,
  captures, and all connectors (email/calendar/tasks raise `PermissionError` in
  public mode). Sensitive-tagged notes are dropped entirely.

## REST API (per authenticated user)

```
# core
POST /api/vault/import   GET /api/vault/import/{job}   GET /api/vault   DELETE /api/vault
POST /api/query          POST /api/ask                 POST /api/collab/ask
# product
GET  /api/profile                 GET  /api/portfolio
GET  /api/dashboard
GET  /api/agents   POST /api/agents/{agent}/enable|disable
GET  /api/graph/viz               GET  /api/suggestions
# knowledge
POST /api/knowledge/build|ask|search   GET /api/knowledge/metadata|graph
# collaboration
POST /api/goals  GET /api/goals  POST /api/goals/{id}/milestones  POST /api/milestones/{id}/complete
GET  /api/reflect   GET /api/learn   GET /api/memory   GET /api/cards
# connectors (PRIVATE only)
GET  /api/connectors            GET /api/connectors/{email|calendar|tasks}?mode=private
# account
POST /auth/signup|login   GET /api/me   POST /api/settings/openai-key   PUT /api/settings/private-folders
```

## Connectors (Email / Calendar / Tasks)

A pluggable framework (`amy/connectors/`): a `Connector` interface + a working
**local JSON provider** (reads `email.json` / `calendar.json` / `tasks.json` from
the user's connector dir). Real **Gmail / Google Calendar / Google Tasks**
providers implement the same interface and are dropped in via
`registry.register(kind, provider)` — no caller changes. All connectors are
**private-only**: any access in public mode raises `PermissionError`.

> The OAuth-backed Gmail/Google providers are the one remaining real-world
> integration to implement against this interface; the architecture, gating, and
> API are all in place and tested.

## Deployment

- **FastAPI** app: `amy.saas.app:app`.
- **Docker**: `Dockerfile.saas` + `docker-compose.saas.yml` (persistent volume).
- **GitHub Actions**: `.github/workflows/ci.yml` runs the full test suite on push/PR.
- **Railway / Render / Fly**: container + persistent volume + secrets — see `OPERATIONS.md`.

## Tests (offline)

```bash
pytest tests/test_product.py tests/test_connectors.py -v
```

Covers profile, public-portfolio safety (finance/sensitive excluded), marketplace
enable/disable + routing filter, dashboard assembly, suggestions fusion, graph-viz
shape, connector private reads, and public-mode blocking. Verified green.
