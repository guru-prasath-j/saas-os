# Unified Timeline

A single chronological timeline merging events from notes, activities, system
events, decisions, and the connectors (email / calendar / tasks). Extends the
existing `TimelineEngine` (the prior version is preserved; this adds connectors,
grouping, filtering, search, and summaries).

## Overview
`TimelineEngine` collects timestamped items from every source, sorts newest-first,
and supports grouping by day/week/month, filtering by source, keyword search, and
a summary.

## Architecture
```
sources -> TimelineEngine
   activities (collab.db) + events (collab.db) + decisions (collab.db)
   + notes (created/updated) + email/calendar/tasks (connectors)
        |
   build() -> sorted list   grouped(period) -> day|week|month buckets   summary() -> counts/range
```

## Completion: 100% of scope
| Feature | Status |
|---|---|
| Chronological ordering | ✅ newest-first |
| Grouping | ✅ day / week / month |
| Filtering | ✅ `sources=[...]` |
| Search | ✅ `query=` keyword |
| Summaries | ✅ totals, by-source, busiest day, range |

## APIs
```
GET /api/timeline?limit=&source=&q=     # flat list + summary
GET /api/timeline/day                    # grouped by day
GET /api/timeline/week                   # grouped by ISO week
GET /api/timeline/month                  # grouped by month
```

## Example flows
```
GET /api/timeline/day
-> groups: [ {period:"2026-06-22", count:5, items:[…]}, {period:"2026-06-21", …} ]

GET /api/timeline?source=decision,goal&q=budget
-> only decision/goal items mentioning "budget", newest first
```

## Technical debt
- Timestamps are compared as ISO strings; older naive rows sort lexicographically
  (correct in practice, not tz-normalized).
- Connector items only appear when they carry a timestamp; summaries are counts, not
  natural-language recaps (could add an LLM summary per period).
