# Simulation Engine

The Simulation Engine answers "what if?" Given explicit scenario inputs it
computes **deterministic, transparent** projections and a recommendation. There
is no randomness and no ML — every number is derived from your inputs and
explained, so the output is something you can reason about and trust.

## Scenarios

`simulate(scenario, **params)` dispatches to one of four models.

### `job_change`
Inputs: `current_salary`, `new_salary`, `relocation_cost`, `commute_change_min`.
Returns salary delta, percentage change, relocation recoup months, and a
recommendation keyed to the size of the raise.

```json
{"scenario": "job_change", "salary_delta": 25000,
 "salary_change_pct": 25.0, "recommendation": "Strong raise (≥20%)…"}
```

### `financial_change`
Inputs: `monthly_income`, `monthly_expenses`, `one_time_change`,
`monthly_change`, `horizon_months`. Returns current vs new monthly net cash
flow, the projected balance change over the horizon, and a sustainability
verdict (flags a deficit).

### `learning_path`
Inputs: `total_hours`, `hours_per_week`, `skill`. Returns estimated weeks and
months to completion, and advises increasing the pace if it's below ~3h/week.

### `project_timeline`
Inputs: `total_tasks`, `completed_tasks`, `tasks_per_week`, `deadline_weeks`.
Returns remaining tasks, estimated weeks, an `on_track` flag, and (if behind)
the throughput needed to hit the deadline.

## Relationship to the Predictive Engine

| Engine      | Question                                   | Input source        |
|-------------|--------------------------------------------|---------------------|
| Predictive  | "Where am I heading if nothing changes?"   | your stored history |
| Simulation  | "What happens if I change *this*?"         | explicit parameters |

Predictive extrapolates the past; Simulation models a hypothetical you specify.

## API

```
POST /api/simulate
{ "scenario": "job_change", "params": { "current_salary": 100000, "new_salary": 125000 } }
```

Unknown scenarios return an `error` plus the list of valid scenario names.

## Dependencies

Pure functions of their inputs. A collab DB can be passed for future defaults
(e.g. pulling your current salary) but none is required.
