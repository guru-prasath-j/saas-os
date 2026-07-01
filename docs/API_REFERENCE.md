# PersonalOS API Reference

All endpoints require `Authorization: Bearer <token>` in the request header, **except** `POST /auth/signup`, `POST /auth/login`, and the Google OAuth callback (`GET /api/connectors/google/callback`).

**Base URL:** `http://127.0.0.1:8849`

Every JSON response that signals a client error includes a `detail` field. HTTP status codes follow REST conventions: `200 OK`, `400 Bad Request`, `401 Unauthorized`, `403 Forbidden`, `404 Not Found`, `409 Conflict`, `422 Unprocessable Entity`.

---

## Quick start

Sign up, log in, and ask your first question in three calls:

```bash
# 1. Create account
curl -s -X POST http://127.0.0.1:8849/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{"email":"guruprasath@example.com","password":"mypassword123"}' | jq .

# → {"token": "eyJ...", "user": {"id": "...", "email": "..."}}

# 2. Store the token
TOKEN="eyJ..."

# 3. Ask Amy
curl -s -X POST http://127.0.0.1:8849/api/collab/ask \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"text":"Can I afford a ₹20,000 MacBook stand?"}' | jq .answer
```

---

## Table of contents

1. [Auth & Account settings](#1-auth--account-settings)
2. [Vault](#2-vault)
3. [Chat (Collab)](#3-chat-collab)
4. [Memory Lake](#4-memory-lake)
5. [Knowledge Graph](#5-knowledge-graph)
6. [Goals & Planning](#6-goals--planning)
7. [Decisions](#7-decisions)
8. [Timeline](#8-timeline)
9. [Digital Twin & Intelligence](#9-digital-twin--intelligence)
10. [Habits](#10-habits)
11. [Spaced Repetition (SRS)](#11-spaced-repetition-srs)
12. [Entities](#12-entities)
13. [Tags](#13-tags)
14. [Finance](#14-finance)
15. [Google Connector](#15-google-connector)
16. [Operational Layer](#16-operational-layer)
17. [Portfolio & Product](#17-portfolio--product)
18. [Settings & Account](#18-settings--account)

---

## 1. Auth & Account settings

These endpoints manage user registration, login, and personal settings. The `/auth/*` routes are unauthenticated; everything else needs a bearer token.

### POST /auth/signup

Create a new account.

**Body**
| Field | Type | Required |
|-------|------|----------|
| email | string | yes |
| password | string (≥8 chars) | yes |

**Response** `200`
```json
{
  "token": "eyJ...",
  "user": {"id": "uuid", "email": "guruprasath@example.com"}
}
```

**Errors** `409` email already registered · `400` password too short

**Example**
```js
const r = await fetch('/auth/signup', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({email: 'guru@example.com', password: 'secret123'})
});
const {token} = await r.json();
```

---

### POST /auth/login

Sign in with existing credentials. Returns the same shape as signup.

**Body** same as signup.

**Errors** `401` invalid email or password

---

### GET /api/me

Returns the current user's profile.

**Response** `200`
```json
{
  "id": "uuid",
  "email": "guruprasath@example.com",
  "has_openai_key": true,
  "aa_enabled": true
}
```

---

### POST /api/settings/openai-key

Save or update the user's OpenAI API key (encrypted at rest).

**Body**
```json
{"key": "sk-proj-..."}
```

**Response** `200` `{"ok": true, "has_openai_key": true}`

---

### DELETE /api/settings/openai-key

Remove the stored OpenAI key. Amy falls back to local/global models.

---

### GET /api/settings/private-folders

Return the list of vault folders that are kept local (never sent to OpenAI).

**Response** `200` `{"folders": ["Finance", "Family"]}`

---

### PUT /api/settings/private-folders

Overwrite the list of private folders.

**Body**
```json
{"folders": ["Finance", "Health", "Family"]}
```

---

### GET /api/settings/vault

Return current vault-path configuration.

**Response**
```json
{
  "mode": "local",
  "active_path": "/home/guru/.amy/vault/abc123",
  "cloud_sync": false,
  "cloud_path": null,
  "local_path": null
}
```

---

### POST /api/settings/vault

Change vault sync mode (cloud vs local).

**Body**
```json
{"cloud_sync": true, "cloud_path": "/Users/guru/Library/Mobile Documents/iCloud~md~obsidian/Documents/MyVault"}
```

---

### GET /api/settings/aa-enabled

Returns `{"aa_enabled": true}` — whether Account Aggregator is enabled for this user.

---

### POST /api/settings/aa-enabled

Enable or disable the Account Aggregator feature.

**Body** `{"enabled": false}`

---

## 2. Vault

The vault holds all your Obsidian markdown notes. Import, browse, and delete.

### POST /api/vault/import

Upload a `.zip` of an Obsidian vault. Starts a background import job.

**Body** `multipart/form-data`
| Field | Type | Notes |
|-------|------|-------|
| file | File (.zip) | required |
| replace | bool (query param) | default `true` — wipes existing notes before loading |

**Response** `200`
```json
{"job_id": "uuid", "status": "pending"}
```

Poll `/api/vault/import/{job_id}` to check progress.

**Example**
```js
const fd = new FormData();
fd.append('file', zipFile);
const r = await fetch('/api/vault/import?replace=true', {
  method: 'POST', headers: {Authorization: 'Bearer '+token}, body: fd
});
const {job_id} = await r.json();
```

---

### GET /api/vault/import/{job_id}

Check the status of an import job.

**Response**
```json
{
  "job_id": "uuid",
  "status": "done",
  "markdown_notes": 1234,
  "notes_loaded": 1230,
  "error": null,
  "created_at": "2026-06-28T10:00:00",
  "finished_at": "2026-06-28T10:00:45"
}
```

`status` values: `pending` → `running` → `done` | `failed`

---

### GET /api/vault

Return high-level vault info: note count, detected domain agents, index backend.

**Response**
```json
{
  "notes": 1230,
  "index_backend": "faiss",
  "agents": [
    {"name": "Finance agent", "folder": "Finance", "count": 45},
    {"name": "Career agent",  "folder": "Career",  "count": 120}
  ]
}
```

---

### GET /api/vault/tree

Vault folder tree (folders → subfolders → file counts).

**Response** `{"tree": [{"name": "Finance", "count": 45, "children": [...]}]}`

---

### GET /api/notes

Paginated list of all notes.

**Query params** `limit` (default 100) · `offset` (default 0)

**Response** `{"total": 1230, "notes": [{"path": "...", "title": "...", "tags": [...], "sensitive": false}]}`

---

### DELETE /api/vault

Wipe all notes and data for this user. Irreversible.

---

### DELETE /api/account

Delete the user account entirely (data + database row).

---

### GET /api/stats

Index statistics (note count, embedding count, etc.).

---

### POST /api/query

Ask the local vault engine directly (no multi-agent routing, no LLM). Fast keyword + embedding lookup.

**Body** `{"text": "What do I know about SIPs?"}`

**Response**
```json
{
  "answer": "...",
  "sources": ["Finance/SIP Notes.md"],
  "intent": "question",
  "voice_safe": true,
  "sensitive": false,
  "model": "local"
}
```

---

### POST /api/ask

Route through PKOS (Personal Knowledge OS) master agent. Slightly heavier than `/api/query`.

**Body** same as `/api/query`.

---

### GET /api/vault/analyze

Analyze notes for insights (topics, gaps, patterns).

**Query params** `limit` (default 500)

---

### GET /api/domains

Detected knowledge domains and their note counts.

**Response** `{"domains": [{"name": "finance", "notes": 45}, {"name": "career", "notes": 120}]}`

---

## 3. Chat (Collab)

The main conversational interface. Routes your message to the right domain agents (finance, career, health, …) and merges their answers with sources from your vault.

### POST /api/collab/ask/stream

**Streaming version** — returns a `text/event-stream` (SSE). Each event is `data: {...}\n\n`.

**Body** `{"text": "Can I afford a MacBook Pro this month?"}`

**Event types**
| type | data |
|------|------|
| `status` | `"thinking"` — Amy is routing the question |
| `done` | full answer object (see below) |
| `error` | error message string |

**Done event shape**
```json
{
  "answer": "Based on your cashflow...",
  "domains": ["finance"],
  "sources": ["Finance/Budget 2026.md"],
  "confidence": 87
}
```

**Example**
```js
const r = await fetch('/api/collab/ask/stream', {
  method: 'POST',
  headers: {'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'},
  body: JSON.stringify({text: 'What are my top spending categories?'})
});
const reader = r.body.getReader();
// ... read SSE lines
```

---

### POST /api/collab/ask

Non-streaming fallback. Same body and response shape as the `done` event above.

---

### GET /api/memory

Quick snapshot of what Amy remembers about you (preferences, last seen items).

---

### POST /api/memory/pref

Set a preference key-value pair in Amy's memory.

**Body** `{"key": "currency", "value": "INR"}`

---

## 4. Memory Lake

The memory lake stores daily notes, recall, and patterns derived from your activity with Amy. Think of it as Amy's episodic memory.

### POST /api/memory/sync

Trigger an immediate write of today's journal (normally done automatically after every chat).

---

### GET /api/memory/daily

Read the daily note for a given date.

**Query params** `date` ISO date string (default: today UTC)

**Response** `{"date": "2026-06-28", "exists": true, "content": "# 2026-06-28\n..."}`

---

### GET /api/memory/recall

Semantic recall: find the most relevant memory chunks for a query.

**Query params** `q` (required) · `k` (default 3, number of results)

**Response** `{"query": "...", "hits": [{"text": "...", "source": "...", "score": 0.87}]}`

**Example**
```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8849/api/memory/recall?q=SIP+investments&k=5"
```

---

### POST /api/memory/consolidate

Consolidate daily notes into a weekly summary. Runs automatically every Sunday.

---

### GET /api/memory/patterns

Returns recurring patterns detected in your weekly notes (topics, moods, productivity).

---

### GET /api/memory/verify

Verify that the vault files on disk match what is indexed in the database.

---

### POST /api/memory/reindex

Force a full reindex of vault files and decisions.

---

### POST /api/memory/log

Alias for `/api/memory/sync` — write today's journal.

---

### GET /api/memory/index

List all memory files (daily notes, weekly consolidations, long-term memory folder).

**Response**
```json
{
  "files": [
    {"path": "00_Daily/2026-06-28.md", "name": "2026-06-28", "folder": "00_Daily", "size": 1024, "modified": 1751030400.0}
  ]
}
```

---

### GET /api/memory/file

Read a single memory file by path.

**Query params** `path` — relative vault path, e.g. `00_Daily/2026-06-28.md`

**Response** `{"path": "...", "exists": true, "content": "..."}`

---

### GET /api/memory/heatmap

Activity heatmap for the last N days.

**Query params** `days` (default 90)

**Response** `{"heatmap": [{"date": "2026-06-28", "count": 14}]}`

---

## 5. Knowledge Graph

Two separate graph systems: the Knowledge layer (per-note embeddings and wikilink relationships) and the Knowledge Graph (KG) layer (typed nodes: person, org, project, topic, goal, event).

### POST /api/knowledge/build

Build or rebuild the knowledge index (embeddings + wikilink graph) from the vault.

**Response** `{"notes": 1230, "relationships": 3450}`

---

### POST /api/knowledge/ask

Semantic question answered from the knowledge index.

**Body**
```json
{"query": "What are my finance goals?", "domain": "finance", "k": 5}
```

---

### POST /api/knowledge/search

Same as ask but returns raw search results without LLM answer generation.

---

### GET /api/knowledge/metadata

List metadata for all indexed notes.

**Query params** `limit` (default 500)

---

### GET /api/knowledge/graph

Return the knowledge relationship graph (edges between notes).

**Response** `{"edges": [...], "agents": [...]}`

---

### POST /api/knowledge/relationship

Add a manual relationship between two notes.

**Body**
```json
{"src_id": "note-uuid", "dst_id": "note-uuid", "rel_type": "depends_on", "weight": 1.0}
```

---

### POST /api/kg/build

Build the global knowledge graph (typed nodes from vault + connectors).

---

### GET /api/kg/nodes

List KG nodes.

**Query params** `type` (filter: person | org | project | topic | goal | event) · `limit` (default 500)

**Response** `{"nodes": [...], "stats": {"total": 850, "by_type": {...}}}`

---

### GET /api/kg/neighbors

Get neighbors of a node.

**Query params** `id` (required) · `rel` (optional relationship type filter)

---

### GET /api/kg/traverse

BFS traversal from a node up to a given depth.

**Query params** `id` (required) · `depth` (default 2)

---

### GET /api/graph/viz

Graph data ready for force-graph rendering.

**Response** `{"nodes": [...], "edges": [...]}`

---

## 6. Goals & Planning

Goals are the backbone of PersonalOS planning. A goal can have milestones (checkboxes) and tasks (autonomy layer). Completing all milestones marks a goal done.

### POST /api/goals

Create a new goal.

**Body**
```json
{"title": "Save ₹5 lakh emergency fund", "domain": "finance", "target_date": "2026-12-31"}
```

**Response** `{"id": "uuid"}`

---

### GET /api/goals

List all goals with their milestones and progress.

**Response**
```json
{
  "goals": [
    {
      "id": "uuid",
      "title": "Save ₹5 lakh emergency fund",
      "domain": "finance",
      "status": "active",
      "progress": 0.34,
      "milestones": [{"id": "uuid", "title": "Open liquid fund account", "done": true}]
    }
  ]
}
```

---

### POST /api/goals/{goal_id}/milestones

Add a milestone to a goal.

**Body** `{"title": "Transfer ₹10,000 to liquid fund"}`

**Response** `{"id": "milestone-uuid", "plan": [...]}`

---

### POST /api/milestones/{milestone_id}/complete

Toggle a milestone done/undone.

**Query params** `done` bool (default true)

---

### POST /api/goals/{goal_id}/finance-target

Link a savings target amount to a finance goal for drift tracking.

**Body**
```json
{"savings_target": 500000, "monthly_savings_category": "Savings"}
```

---

### GET /api/finance/drift

Report how far behind you are on savings for every finance-linked goal.

**Response**
```json
{
  "drift_reports": [
    {"goal_id": "uuid", "title": "Emergency fund", "required_monthly": 25000, "actual_monthly": 15000, "high_drift": true}
  ]
}
```

---

### GET /api/goals/overview

Autonomous goal engine overview (includes tasks).

---

### POST /api/goals/{goal_id}/tasks

Add an atomic task to a goal (finer-grained than a milestone).

**Body** `{"title": "Research ELSS funds"}`

---

### POST /api/tasks/{task_id}/complete

Mark a task done or undone.

**Query params** `done` bool (default true)

---

### POST /api/goals/{goal_id}/depends

Add a dependency between goals (DAG).

**Body** `{"depends_on": "other-goal-uuid"}`

---

### GET /api/reflect

Weekly reflection: progress, gaps, suggestions.

**Query params** `days` (default 7)

**Response**
```json
{"progress": ["Completed SIP setup"], "gaps": ["No workout tracked"], "suggestions": ["Review Finance goals"]}
```

---

### GET /api/learn

Learning trends and recommendations based on recent activity.

**Query params** `window_days` (default 7)

---

## 7. Decisions

Decision journal: log every important choice, record outcomes, and let Amy analyze patterns in how you decide.

### POST /api/decisions

Record a decision.

**Body**
```json
{
  "title": "Switch from Zerodha to Groww",
  "reason": "Lower fees for mutual funds",
  "category": "finance",
  "confidence": 0.78
}
```

`category` options: `personal` | `career` | `finance` | `health` | `learning` | `projects`

**Response** `{"id": "uuid"}`

---

### GET /api/decisions

List decisions.

**Query params** `limit` (default 100)

**Response** `{"decisions": [...]}`

---

### POST /api/decisions/{decision_id}/outcome

Record the outcome once you know how a decision turned out.

**Body** `{"outcome": "Moved ₹50k to Groww, fees dropped 0.2%", "status": "resolved"}`

---

### GET /api/decisions/history

Filter decisions by category.

**Query params** `category` (optional) · `limit` (default 200)

---

### GET /api/decisions/analysis

Aggregate stats: total, resolved, avg confidence, breakdown by category.

**Response**
```json
{
  "total": 42, "resolved": 28, "avg_confidence": 0.71,
  "by_category": {"finance": 12, "career": 8, "personal": 22}
}
```

---

### GET /api/decisions/recommendations

AI-generated recommendations based on your decision history.

**Query params** `category` (optional filter)

**Response** `{"recommendations": ["You tend to be overconfident on career decisions — review past outcomes.", ...]}`

---

### POST /api/simulate

Simulate a scenario using your goals and history.

**Body**
```json
{"scenario": "What if I take a 6-month break to freelance?", "params": {}}
```

---

## 8. Timeline

Your life in chronological order — activities, decisions, emails, calendar events, tasks — grouped by day, week, or month.

### GET /api/timeline

Full timeline (flat, filterable).

**Query params** `limit` (default 100) · `source` (comma-separated: `activity,email,calendar,tasks,decision,note`) · `q` (text filter)

**Response**
```json
{
  "timeline": [{"ts": "2026-06-28T09:00:00", "kind": "email", "text": "...", "source": "gmail"}],
  "summary": {"total": 340, "by_source": {"email": 120, "activity": 80}}
}
```

---

### GET /api/timeline/day

Today's timeline grouped into hour buckets.

### GET /api/timeline/week

This week's timeline grouped by day.

### GET /api/timeline/month

This month's timeline grouped by week.

---

### POST /api/search

Universal search across vault notes, memory, decisions, emails, calendar.

**Body**
```json
{"query": "Zerodha investment", "sources": ["notes", "decisions"], "limit": 10, "offset": 0}
```

**Response** `{"results": [{"title": "...", "path": "...", "snippet": "...", "score": 0.91}]}`

---

### POST /api/recall

Unified memory recall: combines vault, collab memory, and connectors.

**Body** `{"text": "SIP plans 2025"}`

---

## 9. Digital Twin & Intelligence

Your "digital twin" is Amy's model of you — your skills, traits, goals, personality, and predicted future. It powers auto-routing, autopilot, and future-self validation.

### GET /api/twin

Quick twin snapshot.

**Response** `{"skills": [...], "interests": [...], "active_goals": 5}`

---

### POST /api/twin/ask

Ask your digital twin a question about yourself.

**Body** `{"text": "What am I best at?"}`

---

### GET /api/twin/full

Full twin engine snapshot including traits, momentum, engagement metrics.

**Response**
```json
{
  "profile": {"skills": ["Python", "Finance"], "domains": ["career", "finance"]},
  "traits": {
    "focus_areas": ["FIRE", "freelancing"],
    "momentum": ["Python", "investing"],
    "active_goal_count": 3,
    "completed_goal_count": 7,
    "engagement": 84
  }
}
```

---

### POST /api/twin/full/ask

Ask the full twin engine. Heavier than `/api/twin/ask`.

---

### GET /api/personality

Personality profile derived from your notes and decisions.

**Response**
```json
{
  "traits": {"risk_tolerance": "moderate", "decision_style": "analytical", "learning_style": "deep_dive"},
  "priorities": [{"domain": "finance", "score": 9.2}, {"domain": "career", "score": 8.7}]
}
```

---

### POST /api/future-self/validate

Check whether a decision aligns with your long-term goals and values.

**Body**
```json
{"title": "Buy a ₹15 lakh car on EMI", "category": "finance", "reason": "Convenience"}
```

**Response**
```json
{
  "alignment_score": 0.32,
  "verdict": "This conflicts with your FIRE goal and current debt reduction plan.",
  "conflicts": ["Reduces monthly savings by ~₹18,000", "Delays emergency fund by 8 months"]
}
```

---

### GET /api/predict/goals

Forecast which goals are on track vs at risk.

**Response** `{"forecasts": [{"title": "...", "on_track": false, "days_remaining": 90}]}`

---

### GET /api/predict/{metric}

Predict a specific metric. `metric` ∈ `learning` | `career` | `productivity`

---

### GET /api/executive

Executive brief: a concise status report across goals, decisions, and recent activity.

---

### POST /api/autopilot/run

Run the autopilot: Amy creates tasks, marks stale items, and generates suggestions automatically.

**Query params** `dry_run` bool (default false — set to `true` to preview without writing)

---

### GET /api/context

Context engine profile: current focus mode, active domain, recent activities.

---

### POST /api/context/mode

Set context mode manually.

**Body** `{"mode": "deep_work"}` — valid modes: `deep_work` | `browse` | `admin` | `rest`

---

## 10. Habits

Habit tracker with streaks and check-ins. Daily or weekly frequency.

### GET /api/habits

List all habits with streak and today's check-in status.

**Response**
```json
{
  "habits": [
    {"id": "uuid", "title": "Morning run", "frequency": "daily", "streak": 12, "checked_today": false, "color": "#22D3EE"}
  ]
}
```

---

### POST /api/habits

Add a new habit.

**Body**
```json
{"title": "Read 20 min", "frequency": "daily", "color": "#8B5CF6"}
```

`frequency`: `daily` | `weekly`

**Response** `{"id": "uuid"}`

---

### POST /api/habits/{habit_id}/checkin

Mark today's check-in.

**Body**
```json
{"done": true, "date": null, "note": "Ran 5km in Cubbon Park"}
```

`date` defaults to today if null.

---

### DELETE /api/habits/{habit_id}

Archive (soft-delete) a habit.

---

### GET /api/habits/{habit_id}/heatmap

90-day check-in heatmap for a habit.

**Query params** `days` (default 90)

**Response** `{"heatmap": [{"date": "2026-06-28", "done": true}]}`

---

## 11. Spaced Repetition (SRS)

Auto-generates flashcards from your vault notes and schedules reviews using the SM-2 algorithm. Rate each card 0–5 (0 = Again, 3 = Hard, 4 = Good, 5 = Easy).

### POST /api/srs/build

Build flash cards from vault notes. Rerun after importing new notes.

**Response** `{"added": 45, "total": 312}`

---

### GET /api/srs/stats

Card statistics.

**Response** `{"total": 312, "due": 14, "mastered": 189, "new": 23}`

---

### GET /api/srs/due

Get cards due for review today.

**Query params** `limit` (default 20)

**Response** `{"cards": [{"id": "uuid", "front": "What is XIRR?", "back": "Extended IRR that accounts for irregular cashflows.", "note_path": "Finance/Investing.md"}], "stats": {...}}`

---

### POST /api/srs/review

Submit a rating for a card.

**Body**
```json
{"card_id": "uuid", "quality": 4}
```

`quality` 0–5 (SM-2 scale: 0=Again, 1=Fail, 2=Pass (barely), 3=Hard, 4=Good, 5=Easy)

---

## 12. Entities

People, organisations, and topics extracted from your vault via NLP.

### POST /api/entities/build

Run NLP extraction on vault notes.

**Response** `{"extracted": 234}`

---

### GET /api/entities

List extracted entities.

**Query params** `type` (`person` | `org` | `topic` | `wikilink`) · `limit` (default 100) · `min_mentions` (default 2)

**Response**
```json
{
  "entities": [
    {"name": "Zerodha", "type": "org", "mentions": 14},
    {"name": "Guru Prasath", "type": "person", "mentions": 8}
  ]
}
```

---

### GET /api/entities/search

Search entities by name.

**Query params** `q` (required)

---

## 13. Tags

Tags are extracted from the `#tags` in your Obsidian notes. Clicking a tag searches the vault.

### GET /api/tags

List all tags sorted by frequency.

**Response** `{"tags": [{"name": "finance", "count": 45}, {"name": "career", "count": 30}]}`

---

### POST /api/search

Tag search is done via the universal search endpoint. Send `#tagname` as the query:

```js
await fetch('/api/search', {
  method: 'POST',
  headers: {Authorization: 'Bearer '+token, 'Content-Type': 'application/json'},
  body: JSON.stringify({query: '#investing', limit: 20})
});
```

---

## 14. Finance

The Finance module is your personal CFO. It tracks transactions, budgets, subscriptions, investments, and income sources. It can import bank statements via CSV, PDF (LLM-parsed), or Gmail alerts. All amounts are stored in the currency you enter (the UI formats them in ₹).

---

### Overview

#### GET /api/finance/overview

High-level summary: monthly spending, alerts, category breakdown.

**Response**
```json
{
  "monthly_spending": 42500.0,
  "category_breakdown": {"Food": 8200, "Transport": 4500, "Utilities": 2300},
  "alerts": [{"category": "Food", "message": "16% over budget"}]
}
```

---

### Transactions

Transactions are individual spend/income events. Amounts are positive numbers; the sign is implied by context (all transactions in this system are expenses unless you use negative amounts for refunds).

#### POST /api/finance/transactions

Add a transaction.

**Body**
| Field | Type | Default |
|-------|------|---------|
| amount | float | required |
| category | string | `"Uncategorized"` |
| merchant | string | `""` |
| date | string (ISO) | today |
| source | string | `"manual"` |
| notes | string | `""` |

**Example**
```js
await jpost('/api/finance/transactions', {
  amount: 450,
  category: 'Food',
  merchant: 'Swiggy',
  date: '2026-06-28',
  notes: 'Biryani order'
});
```

**Response** `{"id": "uuid"}`

---

#### GET /api/finance/transactions

List transactions with optional filters.

**Query params**
| Param | Type | Notes |
|-------|------|-------|
| limit | int | default 100 |
| category | string | filter by category |
| since | string | ISO date, e.g. `2026-06-01` |
| until | string | ISO date, e.g. `2026-06-30` |

**Response** `{"transactions": [{...}]}`

Each transaction has: `id`, `amount`, `category`, `merchant`, `date`, `source`, `notes`, `account_id`.

---

#### DELETE /api/finance/transactions/{tid}

Delete a transaction by ID.

---

### Budgets

Budgets set a monthly spending limit per category. Amy will alert you when you exceed them.

#### POST /api/finance/budgets

Set (create or update) a monthly budget for a category.

**Body**
```json
{"category": "Food", "monthly_limit": 8000}
```

**Response** `{"ok": true, "category": "Food", "monthly_limit": 8000}`

---

#### GET /api/finance/budgets

List budgets with current-month spending.

**Response**
```json
{
  "budgets": [{"category": "Food", "monthly_limit": 8000}],
  "status": [
    {"category": "Food", "limit": 8000, "spent": 9250, "over": true}
  ]
}
```

---

#### DELETE /api/finance/budgets/{category}

Remove a budget category.

**Example**
```bash
curl -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8849/api/finance/budgets/Food"
```

---

### Subscriptions

Track recurring payments like Netflix, Spotify, gym, etc.

#### POST /api/finance/subscriptions

**Body**
| Field | Type | Default |
|-------|------|---------|
| name | string | required |
| monthly_cost | float | `0` |
| annual_cost | float | `0` |
| renewal_date | string (ISO) | null |
| auto_renew | bool | `true` |
| payment_method | string | `""` |
| status | string | `"active"` |

**Example**
```js
await jpost('/api/finance/subscriptions', {
  name: 'Zerodha PRO',
  monthly_cost: 999,
  renewal_date: '2026-07-15'
});
```

---

#### GET /api/finance/subscriptions

**Query params** `status` (default `"active"`) — pass `""` or `null` for all.

**Response**
```json
{
  "subscriptions": [{"id":"uuid","name":"Netflix","monthly_cost":649,"renewal_date":"2026-07-08"}],
  "monthly_total": 3248
}
```

---

#### GET /api/finance/subscriptions/insights

AI insights about your subscriptions (unused, cheaper alternatives, renewal reminders).

**Response** `{"tips": ["Netflix was last used 23 days ago — consider pausing.", ...]}`

---

#### PATCH /api/finance/subscriptions/{sid}

Partially update a subscription (any field from the POST body).

---

#### DELETE /api/finance/subscriptions/{sid}

Delete a subscription.

---

### Investments

Track mutual funds, stocks, FDs, gold, PPF, NPS, crypto, or any asset.

#### POST /api/finance/investments

**Body**
| Field | Type | Notes |
|-------|------|-------|
| type | string | `mutual_fund` \| `stock` \| `fd` \| `gold` \| `ppf` \| `nps` \| `crypto` \| `other` |
| name | string | e.g. `"Axis Bluechip Fund"` |
| current_value | float | current market value in ₹ |
| cost_basis | float | total amount invested (for gain/loss calculation) |

**Example**
```js
await jpost('/api/finance/investments', {
  type: 'mutual_fund',
  name: 'Parag Parikh Flexi Cap',
  current_value: 125000,
  cost_basis: 100000
});
```

---

#### GET /api/finance/investments

**Response**
```json
{
  "investments": [
    {"id":"uuid","type":"mutual_fund","name":"Parag Parikh Flexi Cap","current_value":125000,"cost_basis":100000}
  ],
  "portfolio": {"total_value": 450000, "total_cost": 380000, "total_gain": 70000, "gain_pct": 18.4}
}
```

---

#### PATCH /api/finance/investments/{iid}

Update current value or cost basis.

**Body** `{"current_value": 130000}`

---

#### DELETE /api/finance/investments/{iid}

---

### Income

Track salary, freelance, rental, or any recurring income source.

#### POST /api/finance/income

**Body**
```json
{"name": "Infosys salary", "type": "salary", "amount": 85000, "recurrence": "monthly"}
```

`type`: `salary` | `freelance` | `rental` | `business` | `other`
`recurrence`: `monthly` | `annual` | `one_time`

---

#### GET /api/finance/income

**Response**
```json
{
  "income_sources": [{"id":"uuid","name":"Infosys salary","amount":85000,"recurrence":"monthly"}],
  "monthly_total": 97000
}
```

---

#### DELETE /api/finance/income/{sid}

---

### Accounts

Bank accounts are the import containers. Each account holds the bank name and sync method, and transactions imported via CSV/PDF/Gmail are linked to it.

#### POST /api/finance/accounts

**Body**
| Field | Type | Default |
|-------|------|---------|
| nickname | string | required |
| bank_name | string | required |
| account_type | string | `"savings"` |
| sync_method | string | `"manual"` |
| meta | dict | `{}` |

`account_type`: `savings` | `current` | `credit` | `investment`

**Example**
```js
await jpost('/api/finance/accounts', {
  nickname: 'SBI Primary',
  bank_name: 'State Bank of India',
  account_type: 'savings'
});
```

---

#### GET /api/finance/accounts

List all accounts.

---

#### GET /api/finance/accounts/{aid}

Get a single account.

---

#### PATCH /api/finance/accounts/{aid}

Update any account field.

---

#### DELETE /api/finance/accounts/{aid}

---

#### GET /api/finance/accounts/{aid}/transactions

Transactions for a specific account.

**Query params** `limit` · `since` · `until`

---

### Afford check

#### POST /api/finance/afford

Ask Amy if you can afford a purchase right now. Amy factors in your income, spending, subscriptions, and finance goals.

**Body**
```json
{"amount": 15000, "description": "noise-cancelling headphones"}
```

**Response**
```json
{
  "verdict": "yes",
  "reason": "You have ₹23,000 surplus this month after all bills.",
  "tips": ["Consider buying refurbished to save ₹4,000."]
}
```

`verdict` values: `yes` | `maybe` | `no`

---

### Cashflow forecast

#### GET /api/finance/forecast/cashflow

Project next 7 days of spending based on the last two 7-day windows.

**Response**
```json
{
  "projected_week_spend": 12500,
  "last_week_avg": 10800,
  "alert": false,
  "note": "Spending trending up 15%."
}
```

`alert: true` when projected spend > (monthly_income / 4) × 1.1

---

### Import

#### CSV import

##### POST /api/finance/accounts/{aid}/upload/csv

Upload a CSV bank statement.

**Body** `multipart/form-data` with field `file`.

**First upload (new bank)** — returns a column mapping preview:
```json
{
  "needs_mapping": true,
  "headers": ["Date", "Description", "Debit", "Credit", "Balance"],
  "sample_rows": [["28/06/2026", "SWIGGY ORDER", "450.00", "", "24550.00"]]
}
```

Save the column map via `POST /api/finance/accounts/{aid}/column-map`:
```json
{
  "column_map": {
    "date": "Date",
    "merchant": "Description",
    "amount": "Debit"
  }
}
```

**Subsequent uploads** (map already saved) — auto-imports:
```json
{"imported": 43, "skipped": 2, "errors": []}
```

**Built-in presets** (no mapping needed): `GET /api/finance/bank-presets` lists formats pre-configured for major Indian banks (SBI, HDFC, ICICI, Axis, Kotak, etc.).

---

#### PDF import (LLM-powered)

##### POST /api/finance/accounts/{aid}/upload/pdf

Upload a PDF bank statement. Text is extracted via PyMuPDF and parsed by the LLM. Works for most Indian bank PDF formats.

**Query params** `password` — for password-protected PDFs (e.g. SBI statements use last 4 digits of mobile)

**Body** `multipart/form-data` with field `file`.

**Response** `{"imported": 38, "skipped": 1, "errors": []}`

---

#### Gmail sync

##### POST /api/finance/accounts/{aid}/sync/gmail

Scan bank-alert and e-statement emails from Gmail and import transactions.

Requires: Google connector already linked (see section 15).

**Query params**
| Param | Type | Notes |
|-------|------|-------|
| since | string | ISO date, e.g. `2026-06-01` |
| until | string | ISO date |
| max_messages | int | default 50 |

**Response** `{"imported": 12, "skipped": 5, "errors": []}`

---

#### Investment CSV import

##### POST /api/finance/accounts/{aid}/upload/investments/csv

Upload a portfolio CSV (mutual fund NAV statement, stock holding export). Same column-mapping flow as bank CSV.

---

#### Column maps

##### POST /api/finance/accounts/{aid}/column-map

Persist a column mapping for this account's bank. Applied automatically on all future uploads.

**Body** `{"column_map": {"date": "Txn Date", "merchant": "Narration", "amount": "Debit Amt"}}`

---

##### GET /api/finance/column-maps

List all saved column maps.

---

##### GET /api/finance/bank-presets

List built-in bank CSV format presets. No auth required.

**Response** `{"presets": [{"bank": "HDFC Bank", "columns": {...}}, ...]}`

---

### Gmail scope status

#### GET /api/finance/gmail/scope-status

Check whether Gmail access is available. The `gmail.readonly` scope is bundled into the Google connector's OAuth flow — no separate consent step needed.

---

### Calendar sync

#### POST /api/finance/calendar/sync

Push bill due-dates and subscription renewal dates into Google Calendar.

**Query params** `days` (default 30 — how many days ahead to schedule)

**Response** `{"created": 4, "skipped": 1, "errors": []}`

---

### Account Aggregator (AA)

Account Aggregators (AA) are RBI-registered entities that let banks share your transaction data with your consent. Amy supports the AA protocol but requires AA credentials to be configured on the server side.

#### GET /api/finance/accounts/{aid}/sync/aa/status

Check AA configuration status, required env vars, and setup instructions.

---

#### POST /api/finance/accounts/{aid}/sync/aa

Initiate an AA data fetch. Returns `503` with setup instructions until AA credentials are configured. Returns `403` if the user has disabled AA in settings.

**Query params** `consent_handle` — AA consent handle (from AA onboarding flow)

---

### Financial goals

#### GET /api/finance/goals

List goals where `domain = "finance"`, enriched with monthly savings required to reach each active goal on time.

**Response**
```json
{
  "goals": [
    {
      "id": "uuid", "title": "Save ₹5 lakh emergency fund",
      "status": "active", "progress": 0.34,
      "target_date": "2026-12-31",
      "monthly_savings_required": 22000
    }
  ]
}
```

---

## 15. Google Connector

Connect Google to sync Gmail, Calendar, and Tasks into your memory lake. The OAuth flow uses the standard browser redirect; the bearer token is passed as a query param and encoded in `state` so the callback can authenticate the user.

### GET /api/connectors/google/status

Check if Google is connected and which services are available.

**Response**
```json
{
  "connected": true,
  "services": ["gmail", "calendar", "tasks"]
}
```

---

### GET /api/connectors/google/auth?token={jwt}

**No Authorization header needed — pass your JWT as `?token=...`**

Redirects the browser to Google's OAuth consent screen. On success, redirects back to the callback URL.

```js
// Redirect the browser:
window.location.href = '/api/connectors/google/auth?token=' + encodeURIComponent(token);
```

---

### GET /api/connectors/google/callback

OAuth callback URL. Handled automatically by Google — do not call this directly. On success, redirects to `/` with a success page, and triggers a background sync.

---

### DELETE /api/connectors/google

Disconnect Google (removes stored credentials). Existing synced data stays in the vault.

---

### POST /api/connectors/google/sync

Force an immediate sync of Gmail, Calendar, and Tasks.

**Response** `{"synced": {"gmail": 12, "calendar": 5, "tasks": 3}}`

---

### GET /api/connectors

List all registered connector kinds.

**Response** `{"connectors": ["gmail", "calendar", "tasks"]}`

---

### GET /api/connectors/{kind}

List items from a connector.

**Query params** `mode` (default `"private"`) · `limit` (default 50)

---

## 16. Operational Layer

The operational layer is an event-sourced view of everything Amy knows: connector state, entity state, and event replay. Useful for debugging and building advanced integrations.

### GET /api/ops/snapshot

Full snapshot of the operational layer: connector status, entity counts, recent events.

---

### GET /api/ops/connectors

Connector status (online/offline/error, last sync time).

---

### POST /api/ops/connectors/health

Run a health check on all connectors.

---

### GET /api/ops/entities

List operational entities (typed records synced from connectors).

**Query params** `kind` · `source` · `limit` (default 100)

---

### POST /api/ops/sync/{kind}

Trigger a sync for a specific connector kind (e.g. `gmail`, `calendar`).

---

### GET /api/ops/replay

Replay the event log.

**Query params** `since` (ISO timestamp) · `types` (comma-separated event types) · `limit` (default 200)

---

## 17. Portfolio & Product

Public-safe views (no finance/health/family data), plus the agent marketplace and dashboard.

### GET /api/portfolio

Public portfolio view: skills, projects, learning roadmap. Safe to share.

**Response**
```json
{
  "skills": ["Python", "FastAPI", "React", "Machine Learning"],
  "projects": [{"title": "PersonalOS", "summary": "AI operating system for knowledge workers"}],
  "roadmap": ["Complete MLOps course", "Ship v2 of PersonalOS"],
  "blocked": ["Finance", "Health", "Family"]
}
```

---

### GET /api/profile

Full private profile (includes all domains).

---

### GET /api/dashboard

Unified dashboard: notes, goals, suggestions, finance summary.

---

### GET /api/agents

Agent marketplace listing.

**Response** `{"agents": [{"agent": "finance_agent", "enabled": true}, ...]}`

---

### POST /api/agents/{agent}/enable

Enable a domain agent so it participates in multi-agent routing.

---

### POST /api/agents/{agent}/disable

Disable a domain agent.

---

### GET /api/suggestions

AI suggestions based on learning trends and goal gaps.

**Query params** `window_days` (default 7)

---

### GET /api/cards

Agent-generated insight cards (short prompts / nudges).

---

### GET /api/digest

Weekly digest combining reflection, learning, goals, and suggestions.

**Query params** `days` (default 7)

---

### GET /api/digest/latest

Latest pre-generated digest event (generated automatically every 24 hours by the background scheduler).

---

### GET /api/health

Health check endpoint. No auth required.

**Response** `{"ok": true, "app": "PersonalOS SaaS", "mode": "saas"}`

---

## 18. Settings & Account

See [Section 1 (Auth & Account settings)](#1-auth--account-settings) for all `/api/settings/*` endpoints and `/api/me`.

Quick reference:

| Endpoint | Purpose |
|----------|---------|
| `GET /api/me` | Who am I + key status |
| `POST /api/settings/openai-key` | Set OpenAI key |
| `DELETE /api/settings/openai-key` | Remove OpenAI key |
| `GET /api/settings/private-folders` | List private (local-only) folders |
| `PUT /api/settings/private-folders` | Update private folders |
| `GET /api/settings/vault` | Vault path config |
| `POST /api/settings/vault` | Update vault path config |
| `GET /api/settings/aa-enabled` | AA toggle state |
| `POST /api/settings/aa-enabled` | Enable/disable AA |
| `DELETE /api/vault` | Wipe all vault data |
| `DELETE /api/account` | Delete account permanently |

---

*Generated from router source files. Base URL: `http://127.0.0.1:8849`. All monetary amounts are in ₹ (Indian Rupee) by convention.*
