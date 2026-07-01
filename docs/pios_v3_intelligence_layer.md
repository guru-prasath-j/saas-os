# PIOS — Intelligence Layer

Adds reflective intelligence on top of the autonomous core: a **Decision journal**
and a **Timeline**. Backward-compatible (new `amy/intelligence/` package + one new
table; nothing rewritten).

> Scope decision: of the four proposed features, only **Decision Engine** and
> **Timeline Engine** were built. **Habit Engine** (quantified-self tracking) and
> **Agent Negotiation** (voting/consensus) were assessed as out-of-scope for a
> personal-knowledge assistant — the existing learning-trends and multi-agent
> merge already cover the useful 80% — and are deferred (see `future_enhancements.md`).

## Overview

- **Decision Engine** — record decisions with reason, domain, confidence; later attach
  an outcome + status. A reviewable decision journal.
- **Timeline Engine** — assemble one chronological view from data already in the
  system: activities, events, decisions, and vault note dates.

## Architecture

```
Decision Engine (amy/intelligence/decisions.py)
   record() / set_outcome() / get() / list()   ── decisions table; emits decision.recorded/.resolved

Timeline Engine (amy/intelligence/timeline.py)
   build(notes)  ── merges + sorts (newest first):
       activities (collab.db) + events (collab.db) + decisions + note created/updated dates
```

Both operate on the per-user `collab.db`, so they're tenant-isolated automatically.

## Completion: 100% of the in-scope features

| Feature | Status |
|---|---|
| Decision Engine — decisions, reasons, outcomes, confidence | ✅ |
| Timeline Engine — chronological timeline | ✅ |
| Habit Engine | ⛔ deferred (out of scope) |
| Agent Negotiation | ⛔ deferred (out of scope) |

## APIs

```
POST /api/decisions                       # {title, reason?, domain?, confidence?}
POST /api/decisions/{id}/outcome          # {outcome, status?}
GET  /api/decisions
GET  /api/timeline?limit=100              # merged chronological view
```

## Example flows

**Decision journal**
```
POST /api/decisions {"title":"Take the Flutter role","reason":"best growth","domain":"career","confidence":0.7}
-> {"id":"ab12…"}                 # status: open, emits decision.recorded
POST /api/decisions/ab12…/outcome {"outcome":"accepted offer","status":"resolved"}
GET  /api/decisions -> [{title:"Take the Flutter role", confidence:0.7, outcome:"accepted offer", status:"resolved"}]
```

**Timeline**
```
GET /api/timeline
-> [ {ts:…, source:"decision", text:"Take the Flutter role"},
     {ts:…, source:"event",    kind:"query.asked", text:"how is my budget"},
     {ts:…, source:"note",     text:"Budget"},
     {ts:…, source:"activity", kind:"query", text:"…"} ]   # newest first
```

## Technical debt

- Timeline merges per-source then sorts by ISO timestamp string; mixed naive/aware
  timestamps from older rows sort lexicographically (fine in practice, not tz-normalized).
- Decision outcomes are free-text (no structured scoring of decision quality).
- Inherits prior debt (duplicate retrieval/router/master, in-process bus, no migrations).

## Verification

```bash
pytest tests/test_intelligence.py -v     # 3 tests
pytest tests/ -v                          # 74 total
```
