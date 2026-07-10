# LIFE_AUTOPILOT.md — Binding spec (Parts L1–L9)

> Source of truth for the LIFE AUTOPILOT phase. Companion to
> `docs/AGENT_PLAN.md` (progress table + commit hashes) and `CLAUDE.md`
> (module layout once parts land). Restructured from the phase brief —
> nothing substantive changed.

## Mission

Extend Amy PersonalOS from finance/career autopilot into day-to-day life:
health targets, behavioral pattern detection, habit auto-tracking, a
wellbeing index, and place-triggered opportunity nudges — built entirely on
existing primitives (geo, patterns, commitments, captures, tool registry +
AGENT_GATE, event bus, MemoryWriter/GraphStore). No parallel goal model, no
parallel inbox, no parallel memory, no diagnostic claims.

Re-read before coding: `amy/geo/` (place events, visits, home cells,
enter/leave hysteresis, coordinates-never-reach-an-LLM rule, existing
errand + spend_caution agents), `amy/patterns.py` (merchant/person
cadences), `amy/automation/drift.py` (category pruning), `amy/commitments/`
(detection heuristics + deadline ladder), `amy/captures.py` (capture
classification, incl. `source='meta-glasses'`), `amy/connectors/mcp_call.py`
(tolerant candidate naming), the habits/goals tables + routers, the
timeline rendering path, `closers.py` (`morning_briefing`), the
`place_learning` job (geo_cells×merchant correlation), and the career
vault-bootstrap pattern (AGENT_PLAN CAREER Part 1) — L1 clones it.

ALL existing global constraints apply (event factory, AGENT_GATE with
`ctx._extras` + dedup keys, sensitive routing, fire-and-forget, single-file
frontend, per-agent kill switches, reasoning strings, no bare
`except: pass`, restart-uvicorn reminder, commit per part).

## Hard rules (violating any = failing the task)

1. **Advisory, never diagnostic.** No generated text may assert the user's
   mental/physical state ("you are stressed/burned out/depressed/anxious"
   forbidden). Maintain a forbidden-phrase list asserted in tests against
   every generated line template.
2. **Estimates, not medical advice.** Targets from Mifflin-St Jeor BMR ×
   activity multipliers + age-band sleep only; formula + inputs always
   displayed; estimate-disclaimer everywhere; never supplements/medication/
   treatment output.
3. **Propose, don't impose.** Every new habit/goal/target/adaptation is
   tier-2 with a mandatory evidence payload ("based on 23 geo events + 11
   transactions"). Auto-completion of ACCEPTED habits is tier 0/1. L9
   nudges are dismissible notifications, never writes.
4. **Own baselines, day-type-matched.** Rolling `AMY_LIFE_BASELINE_WEEKS`
   (8) self-comparison; weekdays vs weekday baseline, weekends vs weekend;
   grace days excluded. Only population data allowed = the published
   formulas in rule 2.
5. **Never a nag.** Prefs-table durable dedup keys (the
   `debrief_prompted_{id}` idiom), `AMY_LIFE_RESUGGEST_DAYS` (21),
   preference_drift category pruning (repeated rejections permanently
   silence a category), grace-day suppression. L9 additionally requires a
   REAL pending need per trigger.
6. **Privacy floor.** Health profile, pattern analysis, capture-meal
   classification = `sensitive=True` (Ollama-only). Coordinates and health
   values NEVER in LLM prompts or event payloads (assert in tests). L9
   trigger decisions are pure local rules — no LLM; phrasing calls get
   place-kind + need summary only, never a location trail.
7. **Honest nulls.** Low-confidence metrics stay NULL (`sleep_window`,
   `meal_calorie_est`); backfill never retro-infers day types pre-geo;
   `health_data` returns `available:False` with no wearable MCP — never
   fabricated.
8. **Grace, not punishment.** Travel/silent days pause streaks, exit
   baselines, mute affected nudges; majority-grace weeks produce NO
   wellbeing line; failing habits get easing proposals — never silent
   death, never auto-archive.

## Data model

`collab.db`, `AutomationStore` lazy-init idiom (same as `learning_focuses`/
`connector_sensor_seen`).

- **`health_profile`** (1/user): `dob_or_age`, `sex`, `height_cm`,
  `weight_kg`, `activity_level`, `weight_log` (JSON series), `constraints`
  (stored/shown, never LLM-diagnosed), provenance per field (vault vs
  manual), timestamps. Resume-style encryption if the security helper fits.
- **`life_metrics`** (1/user/day): `office_minutes`,
  `commute_out/return_minutes`, `left_office_at`, `gym_visits`,
  `home_arrival_at`, `sleep_window_start/end` + `sleep_estimate_min`
  (activity-gap inference; NULL when low confidence), `meals_out`,
  `late_night_orders`, `cafe_spend`, `meeting_count/minutes`,
  `focus_blocks`, `reading_minutes`, `late_night_activity_min`,
  `meal_captures` + `meal_calorie_est` (L8, NULL when unavailable),
  `day_type` (weekday|weekend|away|silent), `grace` (bool). Idempotent
  recompute.
- **`habit_links`**: `habit_id`, `signal_type` (`geo_place_visit` |
  `txn_absence` | `txn_presence` | `reading_minutes` |
  `left_office_before` | `sleep_window_met` | `capture_meal`),
  `signal_params` (JSON), `mode` (`auto_complete` tier 0 |
  `auto_suggest_check` tier 1). Unlinked habits stay fully manual.
- **`wellbeing_weekly`**: `week`, `components` (JSON: value + day-type-
  matched 8-week baseline + direction each), `index_delta`, `line_emitted`.
  NO inferred emotional/medical state stored anywhere.

## Tools

`life_metrics_query` (read), `health_targets` (read, formula shown),
`propose_habit` / `propose_goal` (write → AGENT_GATE tier 2, evidence
mandatory), `complete_habit_check` (tier 0 from an `auto_complete` link,
gated otherwise), `adjust_habit_target` (tier 2, old→new diff).

## Events

Add to `AGENT_RELEVANT_EVENTS`: `life.metrics_computed`,
`life.pattern_detected`, `life.habit_autocompleted`,
`life.wellbeing_week_computed`. Payloads = metric keys/counts only.

## Part L1 — Health bootstrap + targets

Clone the career vault-bootstrap exactly — fuzzy-match a health/personal
folder, parse `sensitive=True`; missing essentials → notification +
Habits-tab empty state listing exactly what's needed; target features
dormant until then; provenance + `vault.note_edited` re-parse (tier 1, diff
shown). `amy/life/targets.py`: deterministic BMR/TDEE/sleep-band/protein/
water math, each offered as a tier-2 habit proposal with the math. Weight
entries later (vault/captures) append `weight_log`; >5% target shift →
tier-2 re-proposal with delta. Never silent adjustment.

## Part L2 — Signal aggregator

`life_metrics_daily` job (00:30, previous day, idempotent) from geo
visits, transactions, calendar, learning activities, capture/app
timestamps. Backfill command from historical data (only signals actually
recorded). Day typing + grace computed HERE, consumed everywhere: away =
≥`AMY_LIFE_TRAVEL_GRACE_DAYS` (2) consecutive zero-home-cell days; silent =
near-zero signals. Grace days: out of baselines, pause streaks, suppress
affected nudges. Timeline gains a daily-metrics strip. Emits
`life.metrics_computed`.

## Part L4 — Auto-completion

`habit_links` evaluated at day-close batch + real-time where events allow
(`place_left` checks "left office by 6" immediately). Absence checks
day-close ONLY. `auto_complete` = tier 0 + timeline marker;
`auto_suggest_check` = tier 1 one-tap undo. UI: "tracked automatically via
{signal}" badges; Add-flow offers linkable signals on matching habit text
(suggestion, not forced).

**Streak grace:** `AMY_LIFE_STREAK_GRACE_PER_WEEK` (1) missed day allowed;
grace days pause streaks.

**Adaptation:** ≥`AMY_LIFE_ADAPT_FAIL_WEEKS` (3) failing weeks → easing
proposal (tier 2, miss-pattern evidence); ≥6 effortless weeks → max ONE
level-up proposal; repeated rejection prefs-silences adaptation for that
habit; never auto-archive.

## Part L3 — Nine inference agents

Each: kill switch `AMY_AGENT_LIFE_<NAME>`, weekly-rollup driven, tier-2
evidence-mandatory proposals, dedup per pattern key; no-push-event agents
use the scan-job pattern (documented no-op subscription + `DEFAULT_JOBS`
scan job — the `meeting_prep_scan` idiom):

- **commute** — office_minutes ↑ vs baseline → leave-by habit auto-checked
  by `place_left`; repeated >9pm arrivals → adjust dinner/sleep targets as
  proposal.
- **meals** — ≥N late-night orders → cook habit checked by `txn_absence`;
  café cadence → home-brew habit with monthly savings shown.
- **sleep** — short sleep-window streaks → wind-down habit + goal;
  post-midnight streaks → device-down habit.
- **activity** — gym visit auto-completes workout; 10+ day absence after
  regularity → ONE gentle re-suggestion per window.
- **reading** — real learning engagement → auto-completed read habit
  replacing manual checkboxes.
- **meeting-load** — ≥6 meetings + 0 focus blocks recurring →
  calendar-block habit; 3 consecutive office weekends → protect-a-weekend
  goal, advisory.
- **admin** — insurance renewals from subscriptions, ITR season from
  jurisdiction packs, checkup cadence → goals with deadlines.
- **seasonal** — calendar-abstraction periods (pack data, not code) →
  period-scoped habit adjustments proposed before the period.
- **social** — extends `person_cadences`: broken rhythm → trackable
  call-X habit proposal.

## Part L9 — Place opportunity triggers

ONE dispatcher agent (`AMY_AGENT_LIFE_OPPORTUNITY`) on
`context.place_entered` (dwell only — existing hysteresis) evaluating a
DATA-DRIVEN rules table (`amy/life/opportunity_rules` — new rule types
must not touch the dispatcher). A trigger with no pending need NEVER
fires. Rules:

- **grocery** — grocery kind + cook habit scheduled + no grocery txn N days.
- **pharmacy** — refill commitment due.
- **return_window** — place matches purchase merchant via `place_learning`
  correlation + open return/warranty commitment.
- **refuel/cadence** — merchant-cadence gap ≥ median + slack.
- **spend_caution extend** — place→budget category ≥
  `AMY_LIFE_SPEND_CAUTION_PCT` (85%) of cap → pre-purchase heads-up.
- **cafe_habit** — café + slipping home-brew habit.
- **subscription_brand** — place fuzzy-matches active sub → using-or-
  cancelling surface.
- **person_proximity** — cadence contact area + broken rhythm.
- **gym_prompt** — gym + usual workout hour + habit unchecked → one-tap
  check, tier 0.
- **office_gap** — ≥45min before first meeting + Plane task due,
  best-effort.
- **travel_mode** — first away-day → travel briefing: grace notice, travel
  commitments, FX line via `fx`/`jurisdictions`.
- **custodial_bank** — bank kind + pending custodial validation.

Anti-nag: prefs dedup `life_opp_{rule}_{place_id}_{need_key}`,
`AMY_LIFE_OPP_MAX_PER_DAY` (3), grace suppression, drift pruning per rule
category (two dismissals → silenced). Every nudge shows its evidence +
dismiss records the drift signal. No-kind places skip (the tag-your-places
flow is the fix, never guessing).

## Part L5 — Wellbeing index

Weekly job; components vs day-type-matched baselines (grace excluded both
sides); majority-grace week → NO line. Threshold crossed → ONE briefing
line max (`AMY_LIFE_WELLBEING_MAX_LINES=1`), observation + option phrasing
("office +6h, sleep −40min, no gym visits — a 10-min wind-down habit is
one option; want it proposed?"), accepting → tier-2 proposal, declining
remembered. Components inspectable via API (click → the week's table).
Terminal-advisory: nothing downstream keys on it.

## Part L8 — Extended signals

- **Meal captures** — food-tagged captures incl. meta-glasses → into
  `meal_captures` + estimate-labeled `meal_calorie_est` vs the L1 target;
  local classification `sensitive=True`; NULL on low confidence;
  `capture_meal` becomes a linkable signal.
- **Commitments crossover** — pharmacy-cadence → refill commitment
  proposal; annual health-checkup commitment — existing commitments tier-2
  path, ladder unchanged.
- **health_data wearable stub** — Health-Connect/Google-Fit-shaped MCP via
  `call_mcp_tool` tolerant naming; honestly `available:False` when none
  registered; when one appears, sleep upgrades to device data with
  per-row provenance `inferred|device`; steps/workouts become new link
  signal types.

## Part L6 — Life review + integration

Monthly vault note `09_Memory/Life Review - {month}` (observed vs
baselines / suggested / accepted / rejected / pruned — the auditable
model-of-you, idempotent per month). Briefing Life section: today's
auto-checks, streaks, ONE pattern insight max, admin deadlines — including
L8/L9 signals. Timeline: metrics strip + auto-completion markers + pattern
annotations.

## Part L7 — UI

`index.html`, existing tab pattern, `api()` helper, locale rules.

- **Habits tab** — "Suggested for you" (pending tier-2 proposals, evidence
  expandable), auto-tracked badges, grace-aware streak view, health-targets
  card (formula + disclaimer).
- **Goals tab** — proposed goals, admin goals with deadline + jurisdiction
  source.
- **Timeline** — strip + markers.
- **Wellbeing line** with inspectable component table.

## Config

`AMY_LIFE_AUTOPILOT` master switch; per-agent
`AMY_AGENT_LIFE_{COMMUTE,MEALS,SLEEP,ACTIVITY,READING,MEETINGS,ADMIN,
SEASONAL,SOCIAL(or extend existing),OPPORTUNITY,CAPTURE_MEALS}`;
`AMY_LIFE_LATE_NIGHT_HOUR=23`, `AMY_LIFE_BASELINE_WEEKS=8`,
`AMY_LIFE_WELLBEING_MAX_LINES=1`, `AMY_LIFE_RESUGGEST_DAYS=21`,
`AMY_LIFE_TRAVEL_GRACE_DAYS=2`, `AMY_LIFE_STREAK_GRACE_PER_WEEK=1`,
`AMY_LIFE_ADAPT_FAIL_WEEKS=3`, `AMY_LIFE_SPEND_CAUTION_PCT=85`,
`AMY_LIFE_OPP_MAX_PER_DAY=3`. Jobs re-check switches at runtime
(`learning_feed_refresh` idiom).

## Tests (all sources mocked)

1. Bootstrap present → correct Mifflin-St Jeor math + sensitive routing /
   missing → dormancy + one ask / edit → tier-1 re-parse with diff.
2. Aggregator seeded day → correct row, idempotent recompute,
   low-confidence sleep NULL.
3. Gym visit auto-completes exactly once tier 0; absence checks only at
   day-close.
4. Inference dedup — same pattern twice → one proposal; declined respects
   resuggest window; drift-pruning silences permanently.
5. Wellbeing — adverse week → exactly one line, forbidden-phrase
   assertion on the text, components via API.
6. Target re-proposal only on >5% shift as tier-2 diff.
7. Sensitive routing + no coordinates/health values in any LLM prompt
   (assert payloads).
8. Life Review sections + idempotent per month.
9. Grace — away streak marks grace, pauses streak, exits baselines,
   suppresses nudges; majority-grace week → no line.
10. Weekend false-positive regression — normal Saturday vs weekend
    baseline flags nothing the all-days baseline would have.
11. Adaptation — 3 failing weeks → one easing proposal; two rejections →
    prefs-silenced; streak survives one miss/week.
12. L8 — food capture increments with estimate label / low-confidence
    NULL; `health_data` no-MCP → `available:False`, nothing fabricated;
    pharmacy cadence → refill commitment proposal.
13. L9 — rule fires only with real need; dedup per rule×place×need across
    repeated entries; daily cap; two dismissals silence category; grace
    day suppresses; no-kind place skips; no location trail in phrasing
    prompts; gym one-tap checks exactly once.

## Build order

FIRST commit = convert `tests/test_reactive_agents.py`'s registered-agent-
set assertion to a set `>=` check (this phase adds ten default-on agents;
that assertion broke on every prior phase). Then:

L1 → L2 (backfill + day-typing + grace) → L4 → L3 → L9 → L5 → L8 → L6 → L7

Commit per part; update `docs/AGENT_PLAN.md` progress table with commit
hashes; update `CLAUDE.md` (`amy/life/` module, tables, jobs, events,
agents, new quirks) and `API_ENDPOINTS.md` as parts land.

## Open decisions (surfaced before coding L1+L2)

1. Sleep-window confidence rule — which activity sources count, NULL
   threshold (honesty over coverage).
2. Place kinds — query actual `geo_places` rows for home/office/gym/
   grocery/pharmacy kinds; if missing, the one-time tag-your-places flow
   is a prerequisite designed into L2 (L9's coverage depends on it).
3. Habit schema fit — do `habits`/`goals` tables need columns, or does
   `habit_links` carry everything.
4. Backfill depth existing data supports.
5. What the captures pipeline already extracts (caption/OCR) before
   designing L8 meal detection.
6. What `place_learning`'s correlation table actually stores before
   designing L9 merchant→place matching.
