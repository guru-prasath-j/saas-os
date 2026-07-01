# PIOS v1 — Core System

**PIOS (Personal Intelligence Operating System)** turns a personal knowledge base
(Obsidian vault + Gmail/Calendar/Tasks) into a multi-agent AI system: it generates
metadata, builds a semantic + relationship index, routes questions to domain agents,
and answers with source attribution. Multi-tenant SaaS, per-user isolation.

## Overview

- One backend, many users; each user has an isolated vault, index, and stores.
- Data sources feed a knowledge layer (metadata + embeddings + graph).
- A master agent routes each query to domain agents, merges, and attributes sources.
- Privacy: notes the user marks private route to a **local model only**, never cloud.

## Architecture

```
Data sources ─┐
  Obsidian ───┤
  Gmail ──────┤ (connectors: local | Google)
  Calendar ───┤
  Tasks ──────┘
        │
        ▼
  Vault loader (vault.py)  ──►  Knowledge layer
                                  • metadata.py   (metadata.db)
                                  • embeddings.py (NVIDIA→OpenAI→ST→hashing)
                                  • retrieval.py  (hybrid keyword+vector)
                                  • relationships (graph) + confidence
        │
        ▼
  Agent layer
    • dynamic registry (pkos/registry.py, agent_registry.db)
    • domain agents (per folder/domain, with abstention)
    • intent router (pkos/router.py — single + multi-intent)
    • master agent (pkos/master.py → merge + source attribution)
    • collaboration (collab/: memory, planner, reflection, learning, events)
        │
        ▼
  API (FastAPI, amy/saas/app.py)  ──►  Web SPA + Flutter app
```

## Completion: ~92%

| Area | Status |
|---|---|
| Obsidian vault source | ✅ 100% |
| Gmail / Calendar / Tasks | ✅ framework + local provider; ✅ real Google providers (need OAuth token) |
| Dynamic agent registry | ✅ 100% |
| Domain agents | ✅ 100% |
| Master agent | ✅ 100% |
| Intent router (single + multi) | ✅ 100% |
| Metadata generation | ✅ 100% |
| Semantic search | ✅ 100% |
| Source attribution | ✅ 100% |

Remaining 8% = live Google OAuth onboarding flow (token must be provisioned per
user) and consolidation of duplicate retrieval/router/master paths (tech debt).

## Implemented features

- **Vault ingestion**: zip import (replace/add), per-user storage, background job.
- **Metadata engine** (`knowledge/metadata.py`): id, title, summary, domain,
  subdomains, entities, keywords, tags, importance, timestamps, embedding_id → `metadata.db`.
- **Embeddings** (`knowledge/embeddings.py`): `make_embedder()` auto-selects
  NVIDIA `nv-embedqa-e5-v5` (free) → OpenAI → sentence-transformers → hashing.
- **Semantic search** (`knowledge/search.py` + `retrieval.py`): hybrid keyword +
  embedding with a relevance/abstention gate; confidence scoring.
- **Relationship graph** (`knowledge/relationships.py`): wikilinks, shared-term, manual `depends_on`.
- **Dynamic agent registry** (`pkos/registry.py`, `dynamic.py`): one agent per
  detected domain + `general` fallback; persisted in `agent_registry.db`.
- **Intent router** (`pkos/router.py`): keyword + LLM, single and multi-intent; never fans out to all on no-match.
- **Master agent** (`pkos/master.py`): routes, invokes agents, drops abstainers, merges, attributes sources.
- **Collaboration layer**: memory (conversation-as-context), planner (goals),
  reflection, learning, agent cards, **event store + bus + triggers**, **digital twin**, **auto digest scheduler**.
- **Connectors** (`connectors/`): pluggable email/calendar/tasks; **local** provider
  (file-based) + **Google** provider (Gmail/Calendar/Tasks, OAuth). Private-only.
- **Source attribution**: every answer returns the note paths used.

## Missing / to provision

- **Google OAuth onboarding**: providers are implemented; each user must drop an
  authorized-user `google_token.json` into their connector dir (no in-app consent
  flow yet). Without it, the local provider is used.
- **Consolidation** of the 3 retrieval implementations and duplicate router/master
  paths (works today; future cleanup).

## APIs (selected)

```
# data sources
POST /api/vault/import        GET /api/vault/import/{job}   GET /api/vault
GET  /api/connectors          GET /api/connectors/{email|calendar|tasks}?mode=private
# agents / ask
POST /api/ask                 POST /api/collab/ask          POST /api/collab/ask/stream
GET  /api/agents              POST /api/agents/{agent}/{enable|disable}
# knowledge
POST /api/knowledge/build|ask|search   GET /api/knowledge/metadata|graph   GET /api/graph/viz
# privacy
GET/PUT /api/settings/private-folders   POST/DELETE /api/settings/openai-key
```
(Full list: see the in-code routes in `amy/saas/app.py`; interactive docs at `/docs`.)

## Data flow

1. **Ingest** — user uploads vault zip → notes stored per-user → optional knowledge build.
2. **Index** — metadata generated, chunks embedded into `vector.db`, relationships built.
3. **Ask** — query → intent router → domain agents (each hybrid-retrieves within scope,
   abstains if irrelevant) → master merges → answer + sources (+ confidence).
4. **Connectors** — email/calendar/tasks fetched on demand (Google if token present,
   else local), private-only.
5. **Privacy** — notes in user-marked private folders are tagged sensitive → routed to
   the local model only.

## Technical debt

- Three retrieval mechanisms (`index.py` Chroma, `knowledge` cosine, `pkos` hybrid) — unify.
- Two routers (`pkos/router.py`, `classifier.py`) and three master variants — consolidate.
- In-process scheduler/event bus — single-instance only; move to a worker/queue for HA.
- `init_db()` creates tables but no migrations — add Alembic for schema evolution.
- PBKDF2 passwords — upgrade to Argon2/bcrypt before launch.

## Dependencies

**Core:** fastapi, uvicorn, python-frontmatter, pydantic, python-dotenv, python-multipart.
**Retrieval/LLM:** chromadb, sentence-transformers, openai (also used for NVIDIA NIM), groq, ollama, numpy.
**SaaS:** SQLAlchemy, PyJWT, cryptography (+ psycopg for Postgres in prod).
**Google connectors (optional):** google-api-python-client, google-auth, google-auth-oauthlib.
**Tests:** pytest (59 passing).

## Verification

```bash
pip install -r requirements.txt -r requirements-saas.txt
pytest tests/ -v          # 59 tests
```
