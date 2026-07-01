# PIOS v3 — Autonomous Execution (Autopilot)

Closes the loop from v2: the Executive Agent no longer just *recommends* — the
**Autopilot** acts on its recommendations, within strict safety rails. Fully
backward-compatible (new `amy/autonomous/autopilot.py`; nothing rewritten).

## Overview

`Autopilot.run()` reads the Executive's brief and performs a bounded set of
**safe, additive, reversible** actions, logging each one.

## What it does each run

1. **Enable needed agents** — for the top active (unblocked) goals, if their domain
   agent is disabled in the marketplace, enable it.
2. **Advance stalled goals** — active + unblocked goals with no milestones and no
   tasks get 3 starter tasks (LLM-generated if a key is set, else a heuristic plan).
3. **Flag conflicts** — overdue / blocked goals are emitted as `conflict.flagged` events.

Each run emits `action.taken` per action + a final `autopilot.run` event, and writes
a memory summary.

## Safety rails (deliberate)

- **Additive & reversible only.** It enables agents, adds planning tasks, and flags
  conflicts. It NEVER disables agents, deletes data, sends messages, or takes any
  financial/irreversible action (the money guardrail from the core still applies).
- **Dry-run preview.** `run(dry_run=True)` (or `?dry_run=true`) returns exactly what
  it *would* do without applying anything.
- **Bounded.** `max_actions` caps work per run; only the top 5 goals are considered
  for agent-enable.
- **Auditable.** Every action is an event in the user's event store + a memory note.

## API

```
POST /api/autopilot/run            # act now (safe/additive)
POST /api/autopilot/run?dry_run=true   # preview only
```
Returns: `{dry_run, count, actions[], priorities[], conflicts[]}`.

## Example flow

```
create_goal("Learn Rust", "learning")          # stalled: no tasks, no milestones
Marketplace.disable("learning_agent")          # agent off

POST /api/autopilot/run
-> actions: [
     {action: enable_agent, target: learning_agent, why: "needed for goal 'Learn Rust'"},
     {action: advance_goal, target: "Learn Rust", added_tasks: ["Define what 'done' means…", …]}
   ]
   # learning_agent is now enabled; the goal now has 3 starter tasks
   # events logged: action.taken (x2), autopilot.run
```

## Completion: 100% of the v3 action-loop scope

| Capability | Status |
|---|---|
| Auto-enable agents the priorities need | ✅ |
| Auto-advance stalled goals (starter tasks) | ✅ |
| Auto-flag conflicts as events | ✅ |
| Dry-run preview + caps + audit log | ✅ |

## Technical debt / not yet

- Autopilot is **manual-trigger** (endpoint). Wiring it into the digest scheduler
  for hands-off daily runs is a one-line addition (call `Autopilot(db).run()` in
  `_run_all_digests`) — left off by default so nothing acts without an explicit call.
- It does not yet *execute* domain work (e.g. draft an email via a connector) — that
  needs write-capable connectors + confirmation, a future step.
- Inherits v1/v2 debt (duplicate retrieval/router/master, in-process bus, no migrations).

## Verification

```bash
pytest tests/test_autonomous.py -v     # 12 tests (incl. 4 autopilot)
pytest tests/ -v                        # 71 total
```
