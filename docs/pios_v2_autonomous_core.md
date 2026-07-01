# PIOS v2 — Autonomous Core

Builds on v1. Adds the pieces that let PIOS act on its own: a real **Event Bus**,
an **Executive Agent** that decides what matters, a **Goal Engine** with tasks +
dependencies, and **Unified Memory** that recalls across every source at once.
Fully backward-compatible — v1 modules are untouched; v2 lives in `amy/autonomous/`.

## Overview

- **Event Bus** — publish/subscribe/unsubscribe over a persistent store.
- **Goal Engine** — goals, milestones, **tasks**, **dependencies** (blocked + cycle-safe), combined progress.
- **Executive Agent** — prioritizes goals, resolves conflicts, coordinates agents, reprioritizes domains.
- **Unified Memory** — one `recall(query)` across Vault, Gmail, Calendar, Tasks, Conversations.

## Architecture

```
Event Bus (amy/events/store.py)
   publish() / subscribe() / unsubscribe()   ── persists to events table, dispatches to handlers

Goal Engine (amy/autonomous/goals.py)
   goals + milestones  ── delegates to collab/planner (unchanged)
   tasks               ── tasks table
   dependencies        ── goal_deps table (cycle guard + is_blocked)
   progress            ── combined milestones + tasks

Executive Agent (amy/autonomous/executive.py)
   prioritize_goals()      ── rank: unblocked + progress + has-next-steps
   resolve_conflicts()     ── blocked chains, domain contention, overdue
   coordinate_agents()     ── top goals -> domain agents (+ enabled/blocked)
   reprioritize_domains()  ── goal priority + learning trends -> domain order  (emits domains.reprioritized)

Unified Memory (amy/autonomous/unified_memory.py)
   recall(query)  ── vault (hybrid search) + conversations (memory) + email/calendar/tasks (connectors)
```

## Completion: 100% of v2 scope

| Feature | Status |
|---|---|
| Event Bus — publish / subscribe / unsubscribe | ✅ |
| Executive Agent — prioritize / conflicts / coordinate / reprioritize | ✅ |
| Goal Engine — goals / milestones / **tasks** / **dependencies** / progress | ✅ |
| Unified Memory — Vault + Gmail + Calendar + Tasks + Conversations | ✅ |

## Missing features / to provision

- **Real Gmail/Calendar/Tasks data** still depends on a per-user Google OAuth token
  (from v1). Without it, Unified Memory reads the local connector stubs. The
  in-app OAuth consent flow is still not built.
- Executive is **advisory** — it recommends priority/coordination but does not yet
  auto-execute (e.g. auto-enable agents or auto-run goals). Closing that loop is v3.

## APIs

```
GET  /api/goals/overview                 # goals + tasks + deps + blocked + progress
POST /api/goals/{goal_id}/tasks          # add task
POST /api/tasks/{task_id}/complete       # complete/undo task
POST /api/goals/{goal_id}/depends        # add dependency (400 on cycle)
GET  /api/executive                      # brief: priorities, conflicts, coordination, domain order
POST /api/recall                         # unified memory recall across all sources
```
Event bus is in-process (Python): `EventStore.publish/subscribe/unsubscribe`.

## Example flows

**1. Plan a multi-step goal with dependencies**
```
create_goal("Launch side project")            -> g2
create_goal("Finish portfolio site")          -> g1
add_dependency(g2, g1)                         # launch depends on portfolio
is_blocked(g2) -> True                         # portfolio not done
add_milestone(g1,...) + complete -> g1 done    -> is_blocked(g2) -> False
```

**2. Executive triage**
```
GET /api/executive
-> priorities: [Finish portfolio (rank 1, in progress), Launch project (blocked)]
   conflicts:  [{type: blocked, goal: Launch project, detail: waiting on: Finish portfolio}]
   coordination: [{goal: Finish portfolio, agent: projects_agent, enabled: true}]
   domain_order: [projects, learning, finance]   # + emits domains.reprioritized
```

**3. Unified recall**
```
POST /api/recall  {"text": "budget"}
-> { vault:[Budget.md], email:[Budget review], calendar:[], tasks:[], conversations:[…], count:3 }
```

**4. Event bus reaction**
```
bus.subscribe("goal.completed", handler)
planner.complete_milestone(...)  # last one -> emits goal.completed -> handler fires
bus.unsubscribe("goal.completed", handler)
```

## Technical debt

- Executive is advisory-only (no auto-execution loop yet).
- Unified Memory ranking is per-source then concatenated — no global cross-source
  relevance score; connector matching is keyword-based (vault uses hybrid embeddings).
- Inherits v1 debt: 3 retrieval paths, duplicate routers/masters, in-process
  event bus/scheduler (single-instance), no Alembic migrations, PBKDF2 passwords.

## Dependencies

Same as v1 (no new required deps). Google connectors optional
(`google-api-python-client`, `google-auth`, `google-auth-oauthlib`).

## Verification

```bash
pytest tests/test_autonomous.py -v     # 8 tests
pytest tests/ -v                        # 67 total
```
