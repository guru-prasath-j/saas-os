# CONTEXT_PLAN.md — Context-Aware PersonalOS

Source of truth for the "physical world" project: making Amy react to where
you are, what you're doing, and what's coming — not just what's in Gmail.
Same governance rails as AGENT_PLAN.md: every agent action carries reasoning,
kill switches per agent, writes go through the approval gate, notifications
dedup within 24h.

The unifying loop (already built, being extended):

```
sensors → EventStore → reactive agents → notifications / Approval Inbox → memory
```

Phases ship independently; each is useful on its own. C1 is the foundation —
every later phase consumes its events.

---

## C1 — Context sensor layer + errand geofencing  ✅ (this phase)

The missing sensor class: **location**. Phone (or browser) posts coordinates;
the server matches them against saved places, tracks enter/leave visits, and
emits context events onto the existing bus.

- `amy/geo/store.py` — `GeoStore` over collab.db:
  `geo_places` (name, kind, lat/lon, radius_m, source manual|learned),
  `geo_visits` (open/closed enter–leave spans), `geo_state` (last fix).
  Haversine matching with hysteresis (enter at ≤ radius, leave at > 1.3×radius)
  so GPS jitter doesn't churn enter/leave events.
- Event types: `context.place_entered` / `context.place_left` /
  `context.location_updated` (the latter emitted only when the place set
  changes — raw pings are state, not events; they'd flood the events table).
- Router `amy/saas/routers/geo.py`, prefix `/api/context`:
  - `POST /api/context/location` {lat, lon, accuracy_m?, source?} — the sensor inlet
  - `GET  /api/context/status` — last fix, places currently inside, open visits
  - `POST/GET /api/context/places`, `PATCH/DELETE /api/context/places/{pid}`
  - `GET  /api/context/visits`
  - `PATCH /api/context/tasks/{tid}/place-tag` — tag an open task with a place kind
- **Errand agent** (`_errand_agent`, amy/agents/reactive.py): on
  `context.place_entered`, matches open collab tasks by `place_tag` or by
  keyword (place kind/name token in task title) → notification
  ("You're near X — open task: buy groceries"). Kill switch `AMY_AGENT_ERRAND`.
  Dedup: one reminder per task+place per 24h.
- `tasks.place_tag` column (collab.db migration).
- Sensor clients: any HTTP poster works. Interim: browser
  `navigator.geolocation.watchPosition` from the SPA; real one is C8 (Flutter
  background geofencing).

## C2 — Place learning + spend-aware geofencing  ✅

- **Learn places from money** (`amy/geo/learn.py`): unmatched fixes accrue as
  day-level counts on a ~110 m grid (`geo_cells`, 60-day retention — coarse by
  design, movement can't be reconstructed). `suggest_places()` intersects each
  recurring merchant's transaction *days* with each cell's visit days
  (≥3 txn days, ≥2 overlap days, score ≥0.6, skip cells ≤250 m from a saved
  place); day-level because transactions carry dates, not times — and cell
  days are keyed to LOCAL dates to match them (evening IST ≠ same UTC day).
  The `place_learning` job (daily 21:00) proposes winners as tier-2
  `add_place` approvals, deduped per cell+merchant; approval creates a
  `source='learned'` place via the `add_place` executor. Pure set arithmetic,
  no LLM.
- **Spend-aware entry** (in `_budget_agent`): on `context.place_entered`,
  place kind/name tokens (+ `_KIND_BUDGET_ALIASES`: grocery→Food,
  restaurant→Dining, mall→Shopping…) map to budget categories; a matching
  budget ≥90% consumed → `spend_caution` notification *before* the purchase,
  deduped per category+place per day. Same `AMY_AGENT_BUDGET` kill switch.

## C3 — Commitments engine (deadline-bearing life admin)  ✅

- `amy/commitments/engine.py` — `commitments` table in finance.db (rows
  reference transactions; lazy-created like learned_category_rules).
  Detection is heuristic + local (no LLM): a debit whose merchant tokens hit
  `RETURN_WINDOWS` (amazon 10d, flipkart 7d, myntra 14d, decathlon 90d…)
  opens a return-window row; electronics-ish category OR ≥₹10k debit opens a
  365-day warranty row. Idempotent per (transaction, kind); transfers/EMI/
  custodial categories skipped.
- `commitment_scan` job (daily 08:20): detect → announce new rows (normal) →
  deadline ladder (≤3d high `commitment_due_soon`, 4–14d normal
  `commitment_upcoming` — except return windows, where a 14-day rung is
  noise) → auto-expire past-due rows. All rungs deduped per day.
- Routes: `POST/GET /api/commitments`, `PATCH/DELETE /api/commitments/{cid}`
  (status open|done|dismissed); manual kinds: renewal/document/custom.

## C4 — Life-pattern detection + predictive tasks  ✅

- `amy/patterns.py` — generic `cadence()` (dates → rhythm: ≥4 occurrences in
  120d, median gap ≤45d, gaps clustered within max(2d, 30%)). No LLM.
- `merchant_cadences()` + `pattern_tasks` job (daily 06:30): when a rhythm's
  next cycle is due (due-1d … due+gap), propose an `add_task` through
  `submit_action` at the standard write tier (tier 2 approval by default;
  `AMY_AGENT_WRITE_TIER=1` auto-creates), deduped per merchant+cycle and
  skipped when an open task already matches. The task's `place_tag` is
  prefilled from the saved place whose name/kind shares a token with the
  merchant — so approving it immediately arms the C1 errand agent.

## C5 — Relationship cadence nudges  ✅

- `person_cadences()` — the same cadence math over debits in person
  categories (Transfer/Family/Custodial Disbursement/Gift), keyed by payee.
- `relationship_nudges` job (daily 09:00): rhythm broken (overdue past
  tolerance) → one advisory notification + agent.insight, only within a
  3-day window after the break — never a daily nag. Nothing is written or
  proposed; it's a mirror, not an automation.

## C6 — Universal Approval Inbox (drafts for everything)  ✅

- `/api/inbox/propose` — ANY external system (whatsapp_brain, calendar bot)
  parks a draft as a tier-2 approval (action_type `external_draft`, JWT-auth).
- `/api/inbox/pending?source=` and `/api/inbox/decisions?since=&source=` —
  the proposer polls and acts ONLY on human-approved rows. The executor is an
  acknowledging no-op: execution authority stays with the external system,
  consent stays with the human, audit lives in the same approvals ledger.

## C7 — Future-self ledger (preference drift)  ✅

- `amy/automation/drift.py` + `preference_drift` job (monthly, 2nd 06:45):
  6 months of decided approvals → three signal kinds — `always_reject`
  (≥60% rejected: proposer mis-tuned), `always_approve` (≥5 straight: tier-1
  candidate), `ignored` (≥50% expired unreviewed). Local statistics only;
  one monthly notification + one agent.insight per signal.

## C8 — Mobile sensor client  ◐ (browser interim ✅, Flutter pending)

- **Shipped**: sidebar "Share location" toggle in the SPA —
  `navigator.geolocation.watchPosition` → `POST /api/context/location`,
  throttled to one post/90s, persists via localStorage, resumes after login,
  shows the current place name when inside one.
- **Pending**: Flutter background geofencing (register fences from
  `/api/context/places`, post transitions, on-device push) — needs the
  mobile app codebase, not this repo.

---

### Rails (apply to every phase)

- New agents get `AMY_AGENT_<NAME>` kill switches, default ON.
- Location data is sensitive: it never leaves the box — no LLM call ever
  includes raw coordinates; agents reason over place *names/kinds* only.
- Raw pings are stored as one row of state (`geo_state`), not history;
  history is visits (place-level), keeping the DB small and the data minimal.
- Every notification type deduped via `exists_today`.
