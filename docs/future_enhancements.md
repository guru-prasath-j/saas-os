# PIOS — Future Enhancements

A running backlog of deferred work. Nothing here is required for the current build
(v1 + v2 + v3 are complete and tested, 71 passing); these are the next steps,
grouped by theme. Items are **not yet implemented**.

## Next up (the two open options)

- **(a) Autopilot on a schedule** — wire `Autopilot(db).run()` into the digest
  scheduler for an optional hands-off daily run. Env-gated (e.g. `AMY_AUTOPILOT_ENABLED`,
  off by default) so nothing acts automatically without explicit opt-in. One-line
  addition in `_run_all_digests`.
- **(b) Write-capable connectors** — let Amy *act* on email/calendar/tasks:
  draft a Gmail reply, create a Google Calendar event, add a Google Task — each
  behind a **confirm-before-send** step (propose → user approves → execute), with
  full audit events. Requires write OAuth scopes + a confirmation flow.

## Data sources & connectors

- **In-app Google OAuth consent flow** — `/api/connectors/google/auth` start +
  callback that saves the per-user token, so users link Gmail/Calendar/Tasks with a
  button instead of dropping a `google_token.json` file.
- Write-capable connectors (see (b) above).

## Autonomy (v3+)

- Autopilot scheduler wiring (see (a) above).
- Autopilot executing real domain work (drafting, scheduling) via write connectors + confirm.
- Unified Memory: global cross-source relevance ranking (today: per-source then concatenated;
  connector matches are keyword-based, vault uses hybrid embeddings).

## Retrieval & AI quality

- **Chat-time NVIDIA embeddings + reranking** — currently on hold pending real-world
  quality check. Today chat uses the local hashing embedder; NVIDIA powers only the
  precomputed knowledge store.
- Consolidate the **three retrieval paths** (`index.py` Chroma, `knowledge` cosine,
  `pkos` hybrid) into one.
- Long-term **semantic memory** — embed past conversation turns and retrieve the
  relevant ones (today: last ~3 turns + preferences only).
- Citation-faithfulness check (LLM-as-judge) + calibrated confidence.

## Architecture cleanup (tech debt)

- Consolidate duplicate **routers** (`pkos/router.py` + `classifier.py`) and
  **master agents** (`agents/master.py`, `pkos/master.py`, `collab/orchestrator.py`).
- **Alembic migrations** — `init_db()` only creates missing tables; schema changes
  (new columns) need real migrations for existing DBs.
- **Argon2/bcrypt** passwords (currently PBKDF2).
- Move the in-process **event bus + scheduler** to a task queue (Celery/RQ/Arq) for
  multi-instance / HA deployments.
- **pgvector** (or managed vector DB) instead of per-user Chroma/SQLite at scale.

## API / UX

- **True token-level streaming** for chat (provider streaming through the LLM
  router); today `/api/collab/ask/stream` is progressive (status → full answer).
- **3D WebGL knowledge graph** (`3d-force-graph` + UnrealBloom) — rotatable neuron
  galaxy; current graph is 2D additive-glow canvas.
- "**Kept local 🔒**" badge in chat when an answer came from the local model.
- In-app **API reference page**.
- Vendor `force-graph` locally for fully offline graph rendering.

## Mobile (Flutter)

- Parity screens for Reflection, Learning, Agent Marketplace, Graph, Portfolio,
  Executive, Unified Recall (web has them; Flutter has chat/capture/goals/account).
- True **background gallery auto-watch** (native WorkManager) + **offline capture queue**.
- Apply the futuristic web theme (glass, gradients, sidebar/drawer) to Flutter.

## Product / ops

- **Billing & quotas** (deliberately deferred) — plans, metering, Stripe; slots in as
  middleware on existing endpoints.
- CI: the GitHub Actions workflow runs tests; add lint + coverage gates.
- Backups runbook automation for `AMY_SAAS_DATA` + DB.

---

_Last updated after PIOS v3 (Autopilot). Current test suite: 71 passing._
