# Digital Twin Engine

The Digital Twin is one composed, queryable model of *you*. It fuses every data
source PIOS has into a single representation that can answer questions in your
voice and, over time, is intended to become the **primary interface** to the
system — every other engine feeds it, and it speaks for you.

The `DigitalTwinEngine` is **additive**: it builds on the original
`amy/twin/DigitalTwin` (profile + memory + goals + traits) and extends it with
the two sources the original lacked — **habits** and **decisions** — plus a
**personality** profile.

## Data sources fused

```
Vault / Profile · Memory · Goals · Habits · Decisions · Personality
```

| Source       | Provided by                              |
|--------------|------------------------------------------|
| Profile      | `product.ProfileBuilder` (skills/projects/interests) |
| Memory       | `collab.MemoryManager` (prefs, recent activity) |
| Goals        | `collab.PlannerAgent`                    |
| Habits       | derived here from activity cadence + learning trends |
| Decisions    | `engines.DecisionEngine.analyze()`       |
| Personality  | `digital_twin.PersonalityEngine`         |

## Snapshot

`snapshot()` returns the full model:

```json
{
  "profile": {...}, "memory": {...}, "goals": [...], "traits": {...},
  "habits": {"frequent_actions": [...], "frequent_domains": [...],
             "consistent_areas": [...], "activity_volume": 120},
  "decisions": {"resolution_rate": 0.8, "success_rate": 0.7,
                "strong_categories": [...], "weak_categories": [...]},
  "personality": {...}
}
```

## Asking the twin

`ask(question)` builds a fact sheet from the snapshot — skills, focus areas,
habits, decision style, writing style, priorities, active goals — and (if an LLM
is configured) answers **in the user's own voice and style**. With no LLM it
returns the structured fact sheet, so it always works offline.

## API

| Method & path             | Purpose                          |
|---------------------------|----------------------------------|
| `GET  /api/twin/full`     | full snapshot (habits+decisions+personality) |
| `POST /api/twin/full/ask` | ask the twin a question          |

The original `GET /api/twin` and `POST /api/twin/ask` remain unchanged.

## Roadmap

The twin is designed to grow into the front door of PIOS: the Predictive,
Simulation, Decision, and Future-Self engines all become capabilities the twin
can call on your behalf, so a single conversational surface can reason about
your past, present, and possible futures.
