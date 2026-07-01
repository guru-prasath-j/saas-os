# Build Prompt — Turn PersonalOS (Amy) into a Production Multi-Tenant SaaS

> Hand this whole file to your coding agent. It describes the current system, the
> exact target, decisions already made, the work to do (with acceptance criteria),
> and a backlog of futuristic features. Build in the phases given; don't skip the
> security non-negotiables.

---

## 0. Role & objective

You are a senior full-stack engineer. Take the existing single-user **PersonalOS / Amy**
app and turn it into a **production, multi-tenant SaaS** where many users each have
their own private vault, their own AI key, and their own auto-generated folder agents.
Preserve the existing UX and the privacy model. Ship in phases; each phase must be
runnable and tested before the next.

---

## 1. Current system (what already exists — read before changing)

**Backend** (`_Amy/`, Python + FastAPI):
- `main.py` boots `uvicorn` serving `amy.app:app`. Modes: `personal` / `public`.
- `amy/app.py` — REST + WebSocket API and serves the web dashboard at `/`.
  Endpoints: `/api/meta`, `/api/voices`, `/api/voice`, `/api/tts`, `/api/health`,
  `/api/stats`, `/api/dashboard`, `/api/query`, `/api/confirm`, `/ws`, and the
  capture endpoints `/api/captures` (POST/GET) + `/api/captures/image`.
- `amy/config.py` — env-driven config. One global `VAULT` path. Flags include
  `AUTH_TOKEN` (single bearer token) and `DYNAMIC_AGENTS`.
- `amy/vault.py` — loads notes + YAML frontmatter from the vault (`Note` dataclass).
- `amy/index.py` — vector index (Chroma + Ollama/ST embeddings), keyword TF-IDF
  fallback. One global collection `"vault"`.
- `amy/engine.py` — **singleton** `Engine`: loads all notes in memory at startup,
  builds the index lazily, holds the `MasterAgent`. Has `add_capture_note()` hot-reload.
- `amy/agents/master.py` — `MasterAgent` routes a query to one sub-agent. Has
  guardrails (never move money), deterministic count queries, short-term memory,
  write proposals needing confirmation.
- `amy/agents/folders.py` + `base.py` — per-folder sub-agents (personal layout).
- `amy/classifier.py` — keyword-first intent routing (hardcoded keywords).
- `amy/dynamic.py` — **already built**: builds one agent per top-level folder of the
  loaded vault + a `general` fallback + `DynamicClassifier`. Enabled by
  `AMY_DYNAMIC_AGENTS=1`.
- `amy/llm.py` — provider router: Groq → OpenAI → Ollama → template. **Sensitive
  queries go to local Ollama only, never cloud** (the core privacy guarantee).
- `amy/captures.py` — photo ingestion: image → OpenAI vision caption + OCR →
  reverse-geocode → writes a markdown capture note into `08_Captures/` → indexed.
- `amy/auth.py` — optional single bearer token.

**Mobile** (`_Amy/flutter_app/`, Flutter): chat + voice client, photo capture
(camera / share-to-app / gallery sync), settings (server URL + token). Talks to the
same backend.

**Vault**: an Obsidian folder of markdown files. Sensitive data lives in
`02_Family/` and `03_Finances/`; notes can carry a `sensitive` frontmatter tag.

---

## 2. Decisions already made (do not re-litigate)

1. **Model:** fully hosted SaaS — users upload their vault to our servers.
2. **Vault input:** user uploads a **zip of their Obsidian vault**; we import it.
3. **AI cost:** **bring-your-own-key** — each user supplies their own OpenAI key;
   they pay their own usage. We never use a shared key for user content.
4. **Agents:** use the **dynamic agents** already built (`AMY_DYNAMIC_AGENTS`) so
   each user's folders become their agents automatically.

---

## 3. Target architecture (what to build)

```
Flutter app  ─┐                         ┌── Postgres (users, vault metadata, notes index meta, billing)
Web dashboard ┼── HTTPS + JWT ──► FastAPI (multi-tenant) ┼── Vector store, per-user collection (Chroma/pgvector)
              ┘                         ├── Object storage (R2/S3): uploaded zips + capture images
                                        └── Per-user encrypted OpenAI key (KMS / app-level encryption)
```

Everything is scoped by `user_id`. No global vault, no global engine, no shared key.

---

## 4. Work items (with acceptance criteria)

### 4.1 Accounts & auth
- Email/password signup + login (and/or OAuth Google). Issue **JWT** access tokens.
- Replace the single `AUTH_TOKEN` with per-user JWT auth on every `/api` and `/ws`.
- Password hashing (argon2/bcrypt), refresh tokens, logout, password reset.
- **AC:** two users can register; each only ever sees their own data; all endpoints
  reject missing/invalid/expired tokens.

### 4.2 Per-user data model (kill the globals)
- Postgres tables: `users`, `vaults`, `notes` (metadata + path + tags + sensitive),
  `captures`, `api_keys` (encrypted), `usage_events`.
- Remove the global `config.VAULT` and the `Engine` singleton. Make retrieval and
  generation **request-scoped by `user_id`** (or an LRU cache of per-user engines).
- Vector store: **one collection/namespace per user** (Chroma collection
  `vault_{user_id}` or pgvector with a `user_id` filter on every query).
- **AC:** a query from user A can never retrieve user B's notes (write a test that
  proves cross-tenant isolation).

### 4.3 Vault upload & import
- `POST /api/vault/import` accepts a **.zip**; store the raw zip in object storage,
  unpack, parse markdown + frontmatter (reuse `vault.py` logic), write rows, and
  embed into the user's collection. Show import progress/status.
- Re-import / merge / delete-and-replace. Handle large vaults (background job/queue).
- **AC:** uploading a sample Obsidian zip results in searchable notes for that user
  within the import job; re-upload updates without duplicating.

### 4.4 Bring-your-own-key
- `POST /api/settings/openai-key` stores the user's OpenAI key **encrypted at rest**
  (app-level encryption or cloud KMS). Never log it. Use it for that user's vision,
  caption, embeddings, and chat calls.
- Graceful states: no key set, invalid key, quota exceeded.
- **AC:** user A's calls use user A's key; the key is never returned in plaintext via
  any endpoint; with no key, the app prompts the user instead of erroring opaquely.

### 4.5 Dynamic agents (wire the existing module into multi-tenant)
- Turn on `DYNAMIC_AGENTS` for SaaS. Build each user's agents from *their* folders
  via `amy/dynamic.py`. Cache per-user agent sets.
- **AC:** a user with `Work/Health/Recipes` folders gets those three agents +
  `general`; routing matches their folder names.

### 4.6 Per-user privacy & sensitivity
- Sensitivity is per-user: honor the `sensitive` frontmatter tag, plus let the user
  mark folders as private in settings. Keep the "sensitive → local model only" rule,
  but in hosted SaaS the "local model" is **server-side** — decide per plan:
  (a) sensitive notes are excluded from cloud calls entirely, or (b) offered only on
  a private/self-host tier. Document the choice in the privacy policy.
- Encrypt vault contents at rest. Per-user data deletion ("delete my account & data").
- **AC:** a note tagged `sensitive` is never sent to a cloud LLM in the default tier;
  account deletion removes all rows, vectors, zips, and images for that user.

### 4.7 Billing & limits
- Plans (free/pro). Metering via `usage_events`. Rate limits per user/IP.
- Since users BYO-key, charge for the **product** (storage, seats, features), not AI
  tokens. Stripe for subscriptions. Quotas on vault size / captures / requests.
- **AC:** a free user hits a documented limit and is prompted to upgrade; Stripe
  webhook updates plan state.

### 4.8 Mobile & web updates
- Mobile/web: real login screens, account state, vault upload UI, OpenAI-key entry,
  capture + chat scoped to the logged-in user. Point the app at the hosted URL.
- **AC:** the Flutter app logs in, uploads a vault, captures a photo, and asks Amy —
  all against the hosted multi-tenant backend.

### 4.9 Ops & deployment
- Containerize. Deploy backend (Fly.io/Render/your VPS) behind HTTPS. Managed
  Postgres. Object storage (R2/S3). Background worker for imports/embeddings.
- Logging/metrics/error tracking (no secrets in logs). Backups. Health checks.
- **AC:** a clean environment can be stood up from infra-as-code / a documented
  runbook; restart-safe; secrets only via env/secret manager.

---

## 5. Security non-negotiables
- Per-tenant isolation enforced server-side on **every** query (defense in depth).
- All user OpenAI keys encrypted at rest; never logged; never returned.
- HTTPS only; JWT with short expiry + refresh; argon2/bcrypt passwords.
- Vault contents + capture images encrypted at rest; signed URLs for image access.
- GDPR-style data export + delete. A clear privacy policy describing where data and
  keys live and how sensitive notes are handled.
- Keep the guardrail: **never** generate actions that move money.

---

## 6. Suggested phase order
1. Accounts + JWT (4.1) and per-user data model + tenant isolation test (4.2).
2. Vault zip import (4.3) + per-user vector collections.
3. BYO-key (4.4) + dynamic agents wired in (4.5).
4. Per-user privacy/sensitivity + deletion (4.6).
5. Mobile/web login + upload UI (4.8).
6. Billing/limits (4.7) + ops/deploy (4.9).

Each phase: write tests, then a short demo script proving the acceptance criteria.

---

## 7. Deliverables
- Updated backend (multi-tenant), updated Flutter app, infra/runbook, test suite
  (esp. a cross-tenant isolation test), and a short `OPERATIONS.md` + `PRIVACY.md`.

---

# Futuristic & feature ideas (backlog — pick what excites you)

## Capture & context (extends the photo idea)
- **Voice memos & audio notes** — record audio, transcribe (Whisper), index like captures.
- **Document scanner mode** — multi-page receipts/IDs → structured fields (amount, date, vendor) auto-extracted.
- **Auto-tagging & auto-foldering** — the agent files each capture into the right folder and suggests tags.
- **"What changed?" timeline** — a daily/weekly digest of what you captured and learned.
- **Location memory** — "what did I photograph near here before?" using stored GPS.
- **Screenshot inbox** — share screenshots; Amy extracts tasks/links/dates from them.

## Agentic intelligence
- **Proactive agent** — scheduled runs that surface reminders, stale tasks, follow-ups
  (you already have a scheduler concept).
- **Cross-folder reasoning** — a planner agent that combines multiple folder agents to
  answer "prep me for tomorrow" (calendar + tasks + notes + captures).
- **Write-back with confirmation everywhere** — let Amy draft notes, update logs, create
  tasks (your proposal/confirm pattern, generalized).
- **Memory graph** — entity/relationship extraction across notes → a knowledge graph
  view and graph-aware retrieval.
- **Multi-modal answers** — answers that show the actual photo, map pin, or table inline.

## Integrations
- **Calendar / email / WhatsApp / Telegram** ingestion → notes the agent can reason over.
- **Obsidian two-way sync** (git or a community plugin) so edits flow both ways.
- **Browser extension** — clip pages/highlights straight into the vault.
- **Wearable / quick-capture widget** — one-tap photo or voice note from the lock screen.

## Privacy & trust (your differentiator)
- **On-device / private tier** — run sensitive queries against a local model (Ollama)
  on the user's own machine via a bridge, even in SaaS.
- **End-to-end encrypted vaults** — server stores ciphertext; only the user's device
  can read it (hard with server-side AI, but a strong premium tier).
- **Per-note access controls & sharing** — share a single note/folder with a family member.
- **Audit log of every AI access** — show users exactly what was read and when.

## Product & growth
- **Templates marketplace** — prebuilt vault structures (student, founder, freelancer)
  that ship with matching dynamic agents.
- **Team / family vaults** — shared spaces with roles (your `02_Family` concept, multi-user).
- **Multi-LLM choice** — let users pick OpenAI / Anthropic / Groq / local per query.
- **Offline-first mobile** — local queue + sync (already on the roadmap).
- **Analytics dashboard** — captures over time, top topics, knowledge growth.
