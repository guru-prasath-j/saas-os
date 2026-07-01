# Decision Engine (full)

The Decision Engine turns PIOS into a decision journal that learns from itself.
It records the decisions you make, tracks how they turn out, analyzes your
patterns across life domains, and gives you advice grounded in your own history.

It is **layered** and **additive**: the original `amy/intelligence/decisions.py`
journal is unchanged. The full engine writes the **same `decisions` table**, so
the two interoperate — anything recorded by either is visible to both.

## Architecture

```
models/decision_model.py        Decision dataclass + CATEGORIES
repositories/decision_repository.py   data access over the decisions table
engines/decision_engine.py      DecisionEngine: history / analysis / recommend
```

| Layer        | Responsibility                                              |
|--------------|------------------------------------------------------------|
| Model        | the `Decision` shape + the six valid categories            |
| Repository   | all SQL — add / set_outcome / get / all(category)          |
| Engine       | business logic — record, history, analyze, recommend       |

The `decisions` table columns are `id, ts, title, reason, domain, confidence,
outcome, status`. The **`domain` column stores the category** (kept for
backward compatibility with the original journal).

## Fields

| Field        | Meaning                                                  |
|--------------|----------------------------------------------------------|
| decision_id  | short unique id                                          |
| title        | the decision in one line                                 |
| category     | career · finance · health · learning · projects · personal |
| reason       | why you decided this way                                 |
| confidence   | 0–1, how sure you were at the time                       |
| outcome      | free-text note added later (how it went)                |
| status       | `open` until you record an outcome, then `resolved`      |
| timestamp    | when it was recorded                                     |

Invalid categories fall back to `personal`.

## Responsibilities

**Decision history** — `history(category=None)` returns your decisions, newest
first, optionally filtered to one category.

**Outcome tracking** — `set_outcome(decision_id, outcome, status="resolved")`
closes the loop. The free-text outcome is scanned for positive/negative
language to judge whether the decision worked out.

**Decision analysis** — `analyze()` aggregates everything:

```json
{
  "total": 12, "resolved": 9, "open": 3,
  "resolution_rate": 0.75, "success_rate": 0.67, "avg_confidence": 0.71,
  "by_category": {
    "career": {"count": 4, "open": 1, "resolved": 3,
               "success_rate": 0.67, "avg_confidence": 0.8}
  }
}
```

Success rate is computed only over decisions whose outcome could be judged
good/bad, so unresolved decisions never distort it.

**Decision recommendations** — `recommend(category=None)` produces advice from
your own data, e.g. detecting over-confidence (high confidence + low success),
flagging weak categories, and nudging you to close stale open decisions.

## API

| Method & path                          | Purpose                          |
|----------------------------------------|----------------------------------|
| `POST /api/decisions/v2`               | record (with category)           |
| `POST /api/decisions/{id}/outcome`     | track outcome                    |
| `GET  /api/decisions/history`          | history (optional `?category=`)  |
| `GET  /api/decisions/analysis`         | full analysis                    |
| `GET  /api/decisions/recommendations`  | advice (optional `?category=`)   |

The original `POST/GET /api/decisions` endpoints remain for backward
compatibility.

## Design notes

The analysis and recommendations are **transparent heuristics**, not a black
box. Every number can be traced to your records, and recommendations explain
their reasoning, so you can trust (and overrule) them.
