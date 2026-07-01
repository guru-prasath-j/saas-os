# Operational Layer

The Operational Layer is the single source of **live operational state** in the
Personal OS. It sits between the connectors/sensors and the rest of the system,
and it does three things the event log alone could not: it keeps a **current
snapshot** of external entities, it manages **connector lifecycle + health**, and
it gives agents **one façade** to publish, subscribe, query state, and replay.

It is built **on top of** existing infrastructure — it never duplicates them:

* one Event Bus — `amy.events.EventStore` (the `events` table)
* one per-user DB file — `collab.db` (two new tables only)
* the existing `ConnectorRegistry` and `GitHubSensor`

```
External → Connectors/Sensors → Operational Layer → Event Bus → Memory + Agents
                                      │
                          live entity state (op_entities)
                          connector health (op_connector_state)
```

## Package layout (`amy/operational/`)

| Module | Responsibility |
|--------|----------------|
| `models.py` | `EntityState` (live snapshot), `OperationalEvent` (typed publish helper) |
| `state.py` | `StateStore` / `EntityRegistry` — entities + connector state over collab.db |
| `sensors.py` | `Sensor` base + `SensorRegistry` (generalizes `GitHubSensor`) |
| `connectors.py` | `ConnectorManager` — lifecycle + health over `ConnectorRegistry` |
| `sync.py` | `SyncService` — reconcile external → state, emit delta events |
| `replay.py` | `ReplayService` — re-dispatch persisted events (filtered, since-cursor) |
| `layer.py` | `OperationalLayer` — the façade agents use |
| `scheduler.py` | `run_ops_maintenance()` — connector health tick for the worker |

## Storage (in the existing collab.db)

```sql
op_entities(entity_id PK, kind, source, title, state JSON, updated_at)
op_connector_state(connector PK, status, health, last_sync, cursor, detail)
```

No new database. Both are cleared by `CollabDB.reset()` like every other table.

## The façade

```python
ops = OperationalLayer(collab_db, EventStore(collab_db), connector_dir=...)
ops.publish("github.NEW_COMMIT", {...})          # one bus
ops.subscribe("github.NEW_COMMIT", handler)
ops.entities.list_entities(source="github")      # live state
ops.sync.sync_connector("email")                 # reconcile + emit deltas
ops.connectors.check_all()                        # health
ops.replay(handler, types=["github.NEW_COMMIT"]) # backfill
ops.register_default_sensors()                    # GitHub today
```

## APIs

| Method & path | Purpose |
|---------------|---------|
| `GET  /api/ops/snapshot` | connectors + sensors + entity/event counts |
| `GET  /api/ops/connectors` | connector status list |
| `POST /api/ops/connectors/health` | probe + record health |
| `GET  /api/ops/entities?kind=&source=` | live entity registry |
| `POST /api/ops/sync/{kind}` | pull a connector → reconcile → emit deltas |
| `GET  /api/ops/replay?since=&types=` | persisted event feed |

## How agents use it

Agents never touch connectors directly. A sensor ingests external truth →
publishes events + upserts entity state; an agent **subscribes** to the events it
cares about and **reads** current entity state for context. The Memory Writer is
already subscribed in parallel, so everything is also journaled to the vault.

| Agent | Consumes (live state) | Subscribes to | Emits |
|-------|----------------------|---------------|-------|
| Career | GitHub activity, calendar interviews | `github.*`, `calendar.*` | `career.application_updated` |
| Finance | email receipts, calendar bills | `email.NEW_MESSAGE` | `finance.transaction_detected` |
| Health | calendar workouts, tasks | `calendar.*`, `tasks.*` | `health.metric_logged` |
| Learning | GitHub commits, tasks | `github.NEW_COMMIT` | `learning.progress_updated` |
| GitHub/Email/Calendar | (fed by their sensors) | — | `github.*` / `email.*` / `calendar.*` |

## Invariants (anti-duplication contract)

* Exactly one event bus (`EventStore`) — the façade only ever delegates to it.
* Exactly one per-user DB file (`collab.db`).
* Connectors/sensors stay decoupled from memory and domain agents.
* Fully additive — `GitHubSensor` keeps its API (now also a `Sensor`); no existing
  module was rewritten; all prior tests pass.

## Scheduling

The existing background digest loop also runs `run_ops_maintenance()` per user
(connector health probe). Full per-connector sync is manual/API-driven so the
loop stays predictable.
