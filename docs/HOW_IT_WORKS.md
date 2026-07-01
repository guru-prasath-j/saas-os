# How PersonalOS (Amy) Works — SaaS Edition

A complete walkthrough of the system architecture: what runs, how a chat reply is
built, how memory is written and read back, and where every piece of data lives.

---

## What it is

PersonalOS (Amy) is a multi-agent AI operating system that runs over your
Obsidian vault. You upload your markdown notes, Amy indexes them into domain
agents (finance, career, family, …), and you chat with it. Every conversation is
journaled back into the vault as dated markdown — the vault is both your knowledge
base and your long-term memory.

The SaaS layer (`amy/saas/app.py`) is a multi-tenant FastAPI server. Each user
gets their own isolated vault, index, vector store, and collab database. No data
ever crosses tenants.

---

## How to run

```bash
# Install dependencies
pip install -r requirements.txt

# Start the server (reads .env automatically)
python -m uvicorn amy.saas.app:app --host 127.0.0.1 --port 8849 --env-file .env

# Open in browser
http://127.0.0.1:8849
```

### Environment variables (`.env`)

```env
# LLM keys (first with a key wins)
OPENAI_API_KEY=sk-...
GROQ_API_KEY=gsk_...

# Local fallback
OLLAMA_HOST=http://localhost:11434

# Google connector (Gmail + Calendar + Tasks)
GOOGLE_CLIENT_ID=xxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxxx

# Digest scheduler interval (default: 24 h)
AMY_DIGEST_INTERVAL_HOURS=24
```

### Setting up Google OAuth

1. Go to [Google Cloud Console](https://console.cloud.google.com) → create a project
2. Search **"oauth"** → Google Auth Platform → **Create OAuth client**
   - Application type: **Web application**
   - Authorised JavaScript origins: `http://127.0.0.1:8849`
   - Authorised redirect URIs: `http://127.0.0.1:8849/api/connectors/google/callback`
3. Enable APIs: **Gmail API**, **Google Calendar API**, **Tasks API** (via API Library)
4. Data Access → Add or remove scopes → manually paste:
   ```
   https://www.googleapis.com/auth/gmail.readonly
   https://www.googleapis.com/auth/calendar.readonly
   https://www.googleapis.com/auth/tasks.readonly
   ```
5. Audience → add your email as a **test user**
6. Copy **Client ID** and **Client Secret** into `.env`
7. Restart server → open Amy → **Account tab** → **Connect Google**

---

## System map

```
Browser (index.html SPA — 17 tabs)
        │
        │  HTTP / SSE
        ▼
FastAPI SaaS server  (port 8849, ~180 routes)
  │
  ├── auth router         /auth/signup  /auth/login
  ├── vault router        /api/vault  /api/notes  /api/vault/import  /api/vault/tree
  ├── collab router       /api/collab/ask/stream   ◄── main chat entry
  ├── memory router       /api/memory/*
  ├── knowledge router    /api/knowledge/*  /api/graph/*
  ├── intelligence router /api/goals  /api/decisions  /api/timeline  /api/twin
  ├── connectors router   /api/connectors/google/*  (Gmail + Calendar + Tasks)
  ├── habits router       /api/habits/*
  ├── finance router      /api/finance/*
  ├── events router       /api/events  /api/ops/*
  ├── product router      /api/profile  /api/portfolio  /api/dashboard
  └── captures router     /api/captures
```

Per-user storage lives under `saas_data/<user_id>/`:

```
saas_data/<uid>/
  vault/                  ← your Obsidian markdown notes (source of truth)
    00_Daily/             ← one file per day, written by Amy after every chat
    01_Weekly/            ← weekly rollup (runs automatically every 7 days)
    09_Memory/            ← atomic notes: decisions, goals, captures
  index/
    collab.db             ← SQLite: events, summaries, goals, prefs, activities
    knowledge/            ← vector + metadata + relationship DBs
    graph.db              ← global knowledge graph
    connectors/
      google_token.json   ← Google OAuth token (written after Connect Google)
    habits.db             ← habit check-ins + streaks
    srs.db                ← spaced repetition cards (SM-2)
    entities.db           ← extracted people, orgs, topics, wikilinks
  uploads/                ← temporary zip uploads during import
```

---

## How a chat reply is built

When you send a message via the Chat tab, three memory tiers are combined before
any LLM is called.

```
Your question
      │
      ▼
┌─────────────────────────────────────────────────────┐
│  Tier 1 — Working memory  (always injected, free)   │
│  • Last 3 conversation turns  (collab.db summaries) │
│  • User preferences           (collab.db prefs)     │
└─────────────────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────────────────┐
│  Tier 2 — Episodic recall  (relevance-gated)        │
│  • MemoryRecall searches 00_Daily + 09_Memory       │
│  • Only included if relevance score ≥ 0.15          │
│  • Returns nothing when nothing is relevant         │
│  → Implemented in: amy/memory/recall.py             │
└─────────────────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────────────────┐
│  Tier 3 — Domain knowledge  (PKOS vault search)     │
│  • IntentRouter picks matching domains              │
│  • Each DomainAgent does hybrid search over its     │
│    slice of your vault notes (keyword + embedding)  │
│  • Abstaining agents stay silent (no noise)         │
│  → Implemented in: amy/pkos/                        │
└─────────────────────────────────────────────────────┘
      │
      ▼
LLM (your BYO OpenAI key, or local Ollama, or template)
      │
      ▼
Answer streamed back via SSE
      │
      ▼ (after reply)
JournalSync.sync()  → writes this conversation to 00_Daily/YYYY-MM-DD.md
```

Code path:

```
POST /api/collab/ask/stream
  → CollabMaster.handle()              amy/collab/orchestrator.py
      → MemoryManager.conversation_context()   tier 1
      → MemoryRecall.context_block()           tier 2
      → MasterAgent.handle(extra_context)      tier 3
          → IntentRouter.route()
          → DomainAgent.answer() × N
          → PlannerAgent.plan()   (if query implies planning)
      → memory.add_turn()        store turn for next reply
      → events.emit("query.asked")
  → _journal_user()              auto-sync events → daily note
```

---

## How memory is written

Every significant event is first written to the `events` table in `collab.db`,
then `JournalSync.sync()` reads that table and writes vault markdown.

```
event emitted (e.g. query.asked, goal.created, decision.recorded)
        │
        ▼
  collab.db  events table  (SQLite — every request, lightweight)
        │
        ▼  (triggered automatically after chat, or via /api/memory/log)
  JournalSync.sync()
        │
        ├── append_daily() → 00_Daily/2026-06-28.md
        │     # Timestamped entry with <!-- eid:... --> idempotency marker
        │
        └── write_atomic() → 09_Memory/Decision - <slug>.md
              # Only for high-signal events: decisions, goals, captures
```

The writer is **idempotent**: every entry carries an `<!-- eid:abc123 -->` HTML
comment. If the same event is synced twice (e.g. after a cloud-sync re-read),
the writer checks for the marker and skips — no duplicates ever.

---

## How memory is read back into replies

`MemoryRecall` (tier 2 above) does a hybrid search (keyword + hashing embedder)
over the notes in `00_Daily/` and `09_Memory/` only — not over your regular vault
notes (PKOS already handles those separately). It returns a formatted context block
like:

```
## Relevant memory (from your vault)
- (2026-06-28) Q: Can I afford a trip? A: Based on your finance notes…
- (Decision) Switch to freelance — Category: career, Confidence: 0.8
```

This block is prepended to `extra_context` fed to each domain agent. If nothing
scores above 0.15, the block is empty and nothing is injected — more context is
not always better.

---

## The Memory Lake UI

Navigate to the **Memory Lake** tab in the sidebar (or visit `/memory`).

It has three sub-tabs:

| Sub-tab | What it shows |
|---|---|
| **📄 Files** | All `00_Daily/` and `01_Weekly/` notes grouped by folder — click any to read inline |
| **🕸 Graph** | 2D neuron graph of your vault — nodes are notes, edges are `[[wikilinks]]` |
| **📁 Folders** | Your vault folder tree with note counts per folder |

Sync and weekly consolidation are **fully automatic**:
- After every chat → `JournalSync` writes to `00_Daily/YYYY-MM-DD.md`
- Every 7 days at startup → `Consolidator` rolls up the week into `01_Weekly/`
- No manual buttons needed

## Google Connector

After connecting Google (Account tab → Connect Google), Amy reads:

| Source | What's pulled | Where it appears |
|---|---|---|
| **Gmail** | Last 50 inbox emails (subject, snippet, sender) | Timeline, Universal Search |
| **Google Calendar** | Upcoming events | Timeline tab |
| **Google Tasks** | Open tasks | Timeline tab |

Data is pulled on connect, then re-synced every 24 h via the digest loop.
It feeds into the Timeline tab and Universal Search — not directly into chat
context (privacy boundary: connector data stays in the operational layer).

## UI Tabs (17 total)

| Tab | Purpose |
|---|---|
| Chat | Main 3-tier memory chat |
| Goals | Goal tracker with milestones |
| Reflect | Daily/weekly reflection prompts |
| Learn | Learning tracker |
| Agents | Domain agent marketplace |
| Portfolio | Public-safe shareable view |
| Timeline | Day/week/month event timeline |
| Decisions | Decision journal + analysis |
| Intelligence | Digital twin, personality, predictions, autopilot |
| Memory Lake | Files + Graph + Folders (merged view) |
| Tags | Tag cloud from vault, click to search |
| Habits | Daily habit tracker with streaks |
| Review | Spaced repetition (SM-2) card reviewer |
| People | Entity browser (people, orgs, topics, wikilinks) |
| Import | Upload Obsidian vault zip |
| Account | API keys, privacy, vault settings, Google connect |

---

## Key API endpoints

### Auth
| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/signup` | Create account |
| POST | `/auth/login` | Get JWT token |

### Vault
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/vault/import` | Upload .zip of Obsidian vault |
| GET | `/api/vault` | Vault stats + detected agents |
| DELETE | `/api/vault` | Wipe vault + index |

### Chat
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/collab/ask/stream` | Main chat (SSE, 3-tier memory) |
| POST | `/api/collab/ask` | Same, non-streaming |

### Memory lake
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/memory/log` | Sync events → daily vault note |
| GET | `/api/memory/index` | List all daily/weekly/atomic notes |
| GET | `/api/memory/file?path=` | Read a vault memory file |
| GET | `/api/memory/daily?date=` | Read a specific daily note |
| GET | `/api/memory/recall?q=` | Test episodic recall for a query |
| POST | `/api/memory/sync` | Same as /log (alias) |
| POST | `/api/memory/consolidate` | Build this week's rollup note |
| GET | `/api/memory/patterns` | Weekly learning signal as JSON |
| GET | `/api/memory/verify` | Vault ↔ SQLite drift report |
| POST | `/api/memory/reindex` | Rebuild SQLite from vault markdown |

### Knowledge
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/knowledge/build` | Build vector + metadata + graph |
| POST | `/api/knowledge/ask` | Answer with confidence % + sources |
| GET | `/api/knowledge/graph` | Relationship graph |

---

## Design principles

**Vault-as-truth.** Markdown is canonical; SQLite is a disposable index. If
`collab.db` is deleted, `POST /api/memory/reindex` rebuilds it from the vault.

**Idempotent writes.** Every journal entry carries an `<!-- eid:... -->` marker.
Re-running sync never duplicates.

**Relevance-gated memory.** Tier 2 injects nothing unless a memory actually
scores as relevant. This keeps replies sharp — injecting everything the system
knows would dilute every answer.

**Tenant isolation.** Every API route is scoped by the authenticated user's JWT.
Data paths, SQLite files, and vector collections are all keyed to `user.id`. No
shared state between users.

**BYO key.** Your OpenAI key is stored encrypted at rest. The SaaS server never
uses a shared cloud key — if you haven't set one, the system falls back to a
local Ollama model or a plain-text template answer.
