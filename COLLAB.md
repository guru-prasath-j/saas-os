# Collaboration Layer — Multi-Agent + Memory + Planner + Reflection + Learning

Extends PKOS so multiple agents collaborate, with persistent memory, planning,
reflection and learning. Modular, offline-testable (`amy/collab/`), own SQLite
(`collab.db`). All seven tests pass.

```
User -> CollabMaster -> Memory Manager -> Intent Router -> Multiple Agents -> Planner -> Merge
```

## Components

| File | Role | Spec req |
|---|---|---|
| `orchestrator.py` `CollabMaster` | routes one query to many agents + planner, merges, updates memory/cards | 1 |
| `memory.py` `MemoryManager` | preferences, conversation summaries, recent activities, frequently accessed notes | 2 |
| `planner.py` `PlannerAgent` | goals, milestones, action plans, progress tracking; also a merge participant | 3 |
| `cards.py` `AgentCards` | per-agent: known topics, FAQs, last accessed files, importance | 4 |
| `reflection.py` `ReflectionAgent` | weekly Progress / Gaps / Suggestions | 5 |
| `learning.py` `LearningAgent` | topic trends (increasing/stable/decreasing) + recommendations | 6 |
| `db.py` `CollabDB` | the `collab.db` store | — |

## Multi-agent execution (req 1)

`CollabMaster.handle()` calls the PKOS multi-intent router (already merges domain
agents), then the **Planner joins** when the query implies planning
("afford", "while", "should I", "plan", "switch"…). Example:

> *"Can I afford a Europe trip while switching careers?"*
> → **finance** + **career** + **planner**, merged, with sources.

(Verified in `tests/test_collab.py::test_multi_agent_with_planner`.)

## Memory (req 2)

Preferences (`set_pref`/`get_prefs`), conversation summaries, an activity log, and a
note-access counter. `snapshot()` returns all of it to prime the master.

## Planner (req 3)

`create_goal` → `add_milestone` → `complete_milestone`; progress auto-computes from
milestones done (50% with 1 of 2 done, 100% → status `done`).

## Agent cards (req 4)

Built from each domain agent's notes (top topics, importance = note count); FAQs grow
as questions are routed to that agent; last-files update on access.

## Reflection (req 5) & Learning (req 6)

Reflection summarizes the last N days into Progress / Gaps / Suggestions from the
activity log + goal progress. Learning compares a recent vs prior window and labels
each domain increasing/stable/decreasing, then recommends.

## API (SaaS, per user)

```
POST /api/collab/ask                 -> multi-agent + planner answer
POST /api/goals  GET /api/goals      -> create / list goals (with milestones + progress)
POST /api/goals/{id}/milestones      -> add milestone
POST /api/milestones/{id}/complete   -> mark done (recomputes progress)
GET  /api/reflect                    -> weekly progress / gaps / suggestions
GET  /api/learn                      -> trends + recommendations
GET  /api/memory   POST /api/memory/pref
GET  /api/cards                      -> agent memory cards
```

## Use directly

```python
from amy.vault import load_notes
from amy.collab import CollabMaster
cm = CollabMaster(load_notes("/vault"), "/data/collab.db", llm=None)
print(cm.handle("Can I afford a Europe trip while switching careers?")["domains"])
# -> ['finance', 'career', 'planner']
```

## Tests

```bash
pytest tests/test_collab.py -v   # 7 tests, offline
```

Covers multi-agent+planner merge, memory, goals/milestones/progress, agent cards,
reflection summary, and learning trends (increasing/decreasing).
