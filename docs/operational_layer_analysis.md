# Operational Layer — Architecture Analysis, Design & Roadmap

**Status: analysis only. No implementation has begun. Phase 9 is gated on your approval.**

This document analyzes the existing Personal OS to determine whether an
Operational Layer already exists (in whole or part), then designs how to
introduce a *unifying* Operational Layer that reuses existing infrastructure
without creating duplicate services, storage, or event systems.

---

## Phase 1 — Project analysis (what actually exists)

Module inventory (`amy/`):

| Area | Modules | Relevance to an Operational Layer |
|------|---------|-----------------------------------|
| **Event Bus** | `events/store.py` (`EventStore`), `events/triggers.py`, `events/scheduler.py` | Persisted pub/sub — the operational backbone already exists |
| **Connectors** | `connectors/base.py` (`Connector`, `Item`), `connectors/registry.py` (`ConnectorRegistry`), `local.py`, `google.py` | Pull-based connector abstraction with runtime provider swap + mode gating |
| **Sensors** | `sensors/github_sensor.py`, `github_service.py`, `github_models.py` | Push-based external event ingestion + normalization → publishes to the bus |
| **Memory** | `memory/` (writer, journal, entities, recall, consolidate, reindex) | Consumes events; vault-as-truth memory lake |
| **Knowledge** | `knowledge/` (embeddings, metadata, relationships, retrieval, search, db) | Vector + metadata index over vault notes |
| **Agents** | `agents/` (master, base, folders, career), `dynamic.py`, `pkos/` | Master + dynamic domain agents |
| **State (domain)** | `collab/db.py` tables: `events, goals, tasks, decisions, prefs, agent_state, …` | Per-user SQLite; holds the event log + domain state |
| **Marketplace/state** | `product/marketplace.py` (`agent_state`) | Enable/disable flags = a small state store |
| **Scheduler/worker** | `events/scheduler.py` + `saas/app.py` startup loop (`asyncio` digest loop) | Background worker exists (digest cadence) |
| **APIs** | `saas/app.py` — `/api/events`, `/api/sensors/github/*`, `/api/memory/*`, … | Operational endpoints already partially present |

**Key structural finding:** the building blocks of an Operational Layer already
exist, but they are **fragmented across three packages** (`events/`,
`connectors/`, `sensors/`) with **no unifying facade** and **no live-state
store**. The event *log* exists; a queryable *current-state* view does not.

---

## Phase 2 — Does an Operational Layer already exist? (coverage report)

| Capability | Status | Reuse verdict | Completion |
|------------|--------|---------------|-----------:|
| Event ingestion (external→system) | Exists (`GitHubSensor`) | Reusable; generalize beyond GitHub | **70%** |
| Event normalization | Exists (sensor → canonical `GitHubEvent`) | Reusable; needs a shared Event model | **65%** |
| Event storage | Exists (`events` table via `EventStore`) | Reusable as-is | **95%** |
| Event publication | Exists (`EventStore.publish/emit`) | Reusable as-is | **100%** |
| Event subscriptions | Exists (`subscribe`/`unsubscribe`, `*` wildcard) | Reusable as-is | **90%** |
| Event routing | Partial (by type + `*`; no filters/topics) | Needs refactor (additive) | **40%** |
| Event replay | Partial (`recent()` read; cursor pattern in `JournalSync`) | Needs a generic replay API | **30%** |
| Connector abstraction | Partial (`ConnectorRegistry`, pull `list()`) | Reusable; extend, don't replace | **45%** |
| Connector lifecycle | Missing (no register/start/stop/status) | Missing | **10%** |
| Connector health monitoring | Missing | Missing | **0%** |
| Operational database | Partial (`collab.db` shared) | Reuse the file; add operational tables | **35%** |
| Live entity registry | Missing (no tracked external entities) | Missing | **0%** |
| Live state management | Missing (only event log + domain tables) | Missing | **5%** |
| State synchronization (reconcile) | Missing | Missing | **0%** |
| Operational querying | Partial (`/api/events`, `recent`, `stats`) | Extend | **30%** |
| Operational APIs | Partial (events + sensors + memory) | Extend under one namespace | **35%** |
| Operational caching | Missing (lazy embedder only) | Missing (optional) | **10%** |

### Overall Operational Layer coverage

```
Event Bus / pub-sub-store        ~95%
Event ingestion + normalization  ~67%
Connector framework              ~45%
Event routing / replay           ~35%
Operational APIs / querying      ~33%
Operational database             ~35%
Live entity registry             ~0%
Live state store                 ~3%
State synchronization            ~0%
Connector lifecycle / health     ~5%
Operational caching              ~10%
──────────────────────────────────────
OVERALL OPERATIONAL LAYER       ~38%
```

**Verdict:** an Operational Layer **partially exists** (~38%). The event-driven
core is strong and reusable; what's missing is the *unification* and the
*live-state half* (entity registry, state store, sync, lifecycle/health).

---

## Phase 3 — Current vs target architecture

Target:
```
External Systems → Connectors → Operational Layer → Event Bus → Memory Writer
→ Memory Layer → Knowledge Layer → Context Orchestrator → Master Agent → Dynamic Agents
```

| Target layer | Status today | Notes |
|--------------|--------------|-------|
| External Systems | n/a | GitHub, Google (gated), local files |
| Connectors | **Partial** | `ConnectorRegistry` (pull) + `Sensor` (push) — two half-frameworks |
| **Operational Layer** | **Missing as a unit** | Its *functions* are scattered in `events/`+`sensors/`; no façade, no state store |
| Event Bus | **Exists** | `EventStore` — strong |
| Memory Writer (Journaler) | **Exists** | `memory/journal.py` subscribes to the bus |
| Memory Layer | **Exists** | `memory/` lake (Phases 1–6 already shipped) |
| Knowledge Layer | **Exists** | `knowledge/` |
| Context Orchestrator | **Partial / misplaced** | Context lives in `engines/context_engine.py` + master's own logic; not a distinct orchestrator |
| Master Agent | **Exists** | `agents/master.py` |
| Dynamic Domain Agents | **Exists** | `dynamic.py`, `pkos/` |

**Overlaps / misplaced responsibilities:**
- Connector logic is split between `connectors/` (pull) and `sensors/` (push) —
  they should sit under one Operational Layer.
- The event bus lives in `events/`, but ingestion (`sensors/`) lives separately;
  conceptually both are "operational."
- Memory's `JournalSync` reimplements a cursor/replay pattern that a generic
  operational *replay* should own.
- There is no place that answers "what is the **current** state of my GitHub
  repos / inbox / calendar right now?" — only "what events happened."

---

## Phase 4 — Gap analysis (what blocks a complete Operational Layer)

What prevents completion today, and why each piece matters:

1. **No unifying Operational facade.** Functions are scattered; agents would have
   to import from `events/`, `sensors/`, and `connectors/` separately. *Needed*
   so future agents talk to **one** layer, not three.
2. **No live entity registry.** The system records *events* ("new commit") but
   never the *entities* they concern ("repo `me/piOS`, last commit X, open PRs:2").
   *Needed* so agents can read current state without replaying history.
3. **No live state store.** Current snapshots (latest sync per connector, entity
   states) have nowhere to live. *Needed* for "what's true now" queries and to
   avoid every agent recomputing from the event log.
4. **No connector lifecycle/health.** Connectors can't be registered, started,
   stopped, or health-checked uniformly. *Needed* to run many connectors reliably
   and surface failures.
5. **No state synchronization/reconciliation.** Sensors emit deltas; nothing
   reconciles external truth → local state (e.g. detect a deleted issue). *Needed*
   for correctness as connector count grows.
6. **No generic event replay/routing.** Replay is ad-hoc (`JournalSync`); routing
   is type-or-`*` only. *Needed* so new subscribers (agents, projections) can
   backfill and filter precisely.
7. **Shared, undifferentiated storage.** Operational tables would mix into
   `collab.db` with no namespace. *Needed*: clear operational tables (reusing the
   same per-user DB file — not a new database) so vault-as-truth/memory stay clean.

Missing **abstractions**: `Sensor` base + registry, `Connector` lifecycle,
`OperationalEvent` shared model, `EntityState`. Missing **services**:
`OperationalLayer` facade, `StateStore`, `SyncService`, `HealthMonitor`,
`ReplayService`. Missing **APIs**: `/api/ops/*` (connectors, entities, state,
replay, health). Missing **storage**: `op_entities`, `op_connector_state` tables.

---

## Phase 5 — Operational Layer design (reuse-first)

A new `amy/operational/` package that is a **thin façade + the missing
state half**, explicitly built **on top of** what exists. Nothing is duplicated.

```
amy/operational/
  __init__.py
  events.py        # re-exports EventStore (NO new bus) + a typed publish helper
  connectors.py    # ConnectorManager: wraps ConnectorRegistry + lifecycle/health
  sensors.py       # Sensor base + SensorRegistry (generalizes GitHubSensor)
  state.py         # StateStore + EntityRegistry  (NEW tables in collab.db)
  sync.py          # SyncService: poll connectors/sensors → reconcile → publish
  replay.py        # ReplayService: re-dispatch events from the events table
  layer.py         # OperationalLayer façade: one object agents use
  models.py        # OperationalEvent, EntityState dataclasses
```

Reuse map (the anti-duplication contract):

| Need | Reuse (do NOT rebuild) | Add (thin) |
|------|------------------------|------------|
| Event bus + storage | `events/store.py EventStore` + `events` table | typed publish helper only |
| Connector providers | `connectors/registry.py` + providers | lifecycle/health wrapper |
| External ingestion | `sensors/github_sensor.py` pattern | `Sensor` base + registry to generalize it |
| DB file | `collab.db` (`CollabDB`) | 2 new tables: `op_entities`, `op_connector_state` |
| Memory consumption | `memory/journal.py` (already subscribes) | unchanged |
| Scheduling | `events/scheduler.py` + saas worker loop | add a sync tick |

Responsibilities of the `OperationalLayer` façade:
- **Connector registration & lifecycle** — `register/start/stop/status` over the
  existing `ConnectorRegistry`.
- **Live event ingestion & normalization** — via the generalized `Sensor` base
  (GitHubSensor becomes its first subclass; no rewrite).
- **Operational database** — `op_entities` (live entity snapshots) +
  `op_connector_state` (last sync, cursor, health) in the existing `collab.db`.
- **Live entity registry** — upsert/get/list current entity state.
- **State synchronization** — `SyncService.sync(connector)` reconciles external →
  `op_entities`, emits deltas through the existing bus.
- **Event publication/subscription** — delegates to `EventStore` (one bus).
- **Operational querying & APIs** — `/api/ops/*`.
- **Event replay** — `ReplayService.replay(since, types, handler)` over the
  `events` table (generalizes `JournalSync`'s cursor pattern).
- **Operational caching** — optional in-memory snapshot cache keyed by entity id.
- **Connector health monitoring** — `op_connector_state.health` + `/api/ops/health`.

Design invariants:
- **One bus**: never instantiate a second event system; always `EventStore`.
- **One DB file**: operational tables live in `collab.db`; no new database.
- **Connectors stay decoupled from memory/domains**: connectors → operational →
  bus → (memory, agents). Connectors never import memory or agents.
- **Additive**: existing imports keep working; `sensors.GitHubSensor` keeps its
  current API and simply *also* registers with the new `SensorRegistry`.

---

## Phase 6 — How future agents use the Operational Layer

Every agent interacts through the façade: it **subscribes** to bus events, **reads**
live state from the entity registry, and **publishes** its own operational events.
Connectors never touch agents directly.

| Agent | Publishes (live data / events) | Consumes (live data) | Subscribes to | Emits |
|-------|-------------------------------|----------------------|---------------|-------|
| **GitHub** | repo/PR/issue/CI entity states | — | (it's fed by the GitHub Sensor) | `github.*` |
| **Career** | application status entities | GitHub activity, calendar interviews | `github.NEW_RELEASE`, `calendar.*` | `career.application_updated` |
| **Finance** | account/txn snapshot entities | email receipts, calendar bills | `email.NEW_MESSAGE` (receipts) | `finance.transaction_detected`, `finance.budget_alert` |
| **Health** | habit/metric entities | calendar workouts, tasks | `calendar.*`, `tasks.*` | `health.metric_logged` |
| **Learning** | course/skill progress entities | GitHub commits, tasks | `github.NEW_COMMIT`, `tasks.completed` | `learning.progress_updated` |
| **Shopping** | order/wishlist entities | email order confirmations | `email.NEW_MESSAGE` | `shopping.order_tracked` |
| **Travel** | trip/booking entities | email itineraries, calendar | `email.NEW_MESSAGE`, `calendar.*` | `travel.trip_updated` |
| **Email** | thread/message entities | (fed by Email Sensor) | — | `email.NEW_MESSAGE` |
| **Calendar** | event/meeting entities | (fed by Calendar Sensor) | — | `calendar.NEW_EVENT`, `calendar.REMINDER` |

Pattern: **Sensors** ingest external truth → publish events + upsert entity state;
**Agents** subscribe to the events they care about and read current entity state
for context; the **Memory Writer** (already wired) journals everything to the vault
in parallel. No connector is coupled to any agent or to memory.

---

## Phase 7 — Updated architecture diagram

```
                       ┌──────────────── External Systems ────────────────┐
                       │  GitHub   Gmail   Google Cal   Tasks   Files ...  │
                       └───────────────────────┬──────────────────────────┘
                                                │
                ┌───────────────────────── CONNECTOR LAYER ─────────────────────────┐
                │  ConnectorRegistry (pull)        Sensors (push, normalize)         │
                └───────────────────────────────┬──────────────────────────────────┘
                                                 │  register / lifecycle / health
                ┌──────────────────────── OPERATIONAL LAYER (new façade) ───────────┐
                │  ConnectorManager · SensorRegistry · SyncService · ReplayService   │
                │  StateStore + EntityRegistry  (op_entities, op_connector_state)    │
                └───────────────┬───────────────────────────────┬───────────────────┘
                                │ publish (one bus)              │ read live state
                        ┌───────▼────────┐                       │
                        │   EVENT BUS    │  (EventStore, events table — reused)
                        └───┬────────┬───┘
              subscribe(*)  │        │  subscribe(type)
            ┌───────────────▼─┐   ┌──▼────────────────────────────────────┐
            │ MEMORY WRITER   │   │  Future Domain Agents (Career, Finance,│
            │ (Journaler)     │   │  Health, Learning, GitHub, Email …)    │
            └────────┬────────┘   └──────────────────┬─────────────────────┘
                     │                                 │ read state + memory
            ┌────────▼────────┐              ┌─────────▼──────────┐
            │  MEMORY LAYER    │◄────────────│ CONTEXT ORCHESTRATOR│
            │ (vault: daily/   │   recall    │ (context_engine +   │
            │  memory/weekly)  │────────────►│  memory recall)     │
            └────────┬────────┘              └─────────┬──────────┘
                     │ embed/index                      │ assembled context
            ┌────────▼────────┐              ┌──────────▼──────────┐
            │ KNOWLEDGE LAYER  │─────────────►│    MASTER AGENT     │
            │ (vectors+graph)  │  retrieval   └──────────┬──────────┘
            └─────────────────┘                          │ route
                                              ┌───────────▼──────────┐
                                              │ Dynamic Domain Agents │
                                              └──────────────────────┘
```
Data flow: External → Connectors/Sensors → **Operational Layer** (normalize +
state + publish) → Event Bus → {Memory Writer → Memory → Knowledge, Agents}.
Agents read live state from the Operational Layer and grounded context from
Memory/Knowledge via the Context Orchestrator, then the Master routes to domain
agents. Exactly one bus, one per-user DB file.

---

## Phase 8 — Implementation roadmap (proposed; approval required)

Each phase is additive, test-gated, and verified for "no duplicate bus / no
duplicate DB / Knowledge + Memory still pass."

### OL-1 — Operational models + state store
- **Objective:** `OperationalEvent`, `EntityState`; `StateStore`/`EntityRegistry`
  over 2 new tables in `collab.db`.
- **Create:** `operational/__init__.py, models.py, state.py`; migration in `collab/db.py` (additive `CREATE TABLE IF NOT EXISTS`).
- **Modify:** `collab/db.py` (add tables to schema + reset list).
- **Depends on:** `CollabDB`.
- **Testing:** upsert/get/list entities; isolation per user.
- **Validation:** no new DB file; existing collab tests pass.
- **Risk:** low. **Complexity:** S.

### OL-2 — Sensor base + registry (generalize GitHub)
- **Objective:** `Sensor` base + `SensorRegistry`; make `GitHubSensor` subclass it (keep its current API).
- **Create:** `operational/sensors.py`.
- **Modify:** `sensors/github_sensor.py` (inherit base; no behavior change).
- **Depends on:** OL-1, `EventStore`.
- **Testing:** GitHub sensor still passes; registry registers/lists sensors.
- **Validation:** existing GitHub tests green.
- **Risk:** low. **Complexity:** S.

### OL-3 — Connector lifecycle + health
- **Objective:** `ConnectorManager` wrapping `ConnectorRegistry` with
  register/start/stop/status + `op_connector_state` health writes.
- **Create:** `operational/connectors.py`.
- **Modify:** none (wraps registry).
- **Depends on:** OL-1, `ConnectorRegistry`.
- **Testing:** lifecycle transitions; health recorded.
- **Risk:** low. **Complexity:** M.

### OL-4 — Sync + replay services
- **Objective:** `SyncService.sync()` (reconcile external→`op_entities`, emit deltas via existing bus); `ReplayService.replay(since,types,handler)` over `events` table.
- **Create:** `operational/sync.py, replay.py`.
- **Modify:** optionally refactor `memory/journal.py JournalSync` to *use* ReplayService (kept backward-compatible).
- **Depends on:** OL-1..3.
- **Testing:** sync upserts + emits; replay re-dispatches deterministically; idempotent.
- **Risk:** medium (touches journal). **Complexity:** M.

### OL-5 — OperationalLayer façade + APIs
- **Objective:** single `OperationalLayer` object; `/api/ops/connectors`, `/entities`, `/state`, `/sync`, `/replay`, `/health`.
- **Create:** `operational/layer.py`; endpoints in `saas/app.py`.
- **Depends on:** OL-1..4.
- **Testing:** API smoke; façade wiring.
- **Risk:** low/med. **Complexity:** M.

### OL-6 — Scheduler integration + docs
- **Objective:** add a sync tick to the existing background worker; write `docs/operational_layer.md`.
- **Modify:** `saas/app.py` worker loop, `events/scheduler.py`.
- **Risk:** low. **Complexity:** S.

### OL-7 — Reference agent wiring (proof)
- **Objective:** wire one future agent (e.g. Career) to publish/subscribe/read-state as the template.
- **Risk:** low. **Complexity:** M.

---

## Phase 9 — Incremental implementation

**Not started. Begins only on your approval of this roadmap.** After each OL-phase:
run tests, verify single-bus/single-DB invariants, confirm Knowledge + Memory
suites still pass, confirm compatibility with the Memory roadmap and future
Career/Finance agents, and update docs.
