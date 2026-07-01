# Predictive Engine

The Predictive Engine forecasts where you're heading. It extrapolates from data
already in your collab store (goals, milestones, tasks, activities) using
simple, **explainable** rules — not machine learning. Every forecast carries a
`basis` (the reasoning) and a `confidence` (how much data backed it), so you can
see exactly why a number was produced.

> Honesty note: these are projections from your recent behaviour, not
> guarantees. They answer "if nothing changes, where does this trend lead?"

## Forecasts

**Goal completion** — `forecast_goal(goal_id)` / `forecast_goals()`
Estimates an ETA from progress velocity (`progress ÷ days elapsed`), falling
back to milestone completion rate. Compares the projected date with the goal's
`target_date` to report `on_track`.

```json
{
  "goal_id": "g1", "title": "Ship app", "progress": 0.5,
  "eta_days": 10, "projected_date": "2026-07-07",
  "basis": "50% done over 10d (5.0%/day)", "confidence": 0.5, "on_track": true
}
```

**Learning progress** — `forecast_learning()`
Trend of `learning`-domain activity this week vs last week.

**Career growth** — `forecast_career()`
Trend of `career`-domain activity, plus the nearest career-goal ETA if any.

**Productivity** — `forecast_productivity()`
Trend of *all* activity week-over-week, with a naive linear next-week
projection.

The trend forecasts share one engine and return:

```json
{
  "metric": "productivity", "this_week": 8, "prev_week": 5,
  "trend": "up", "projected_next_week": 11,
  "note": "Overall activity is healthy and trending up.", "confidence": 0.7
}
```

## Confidence

Confidence rises with more data points and a longer observation span (capped at
0.9). A forecast built on two activities over two days is explicitly low
confidence.

## API

| Method & path              | Purpose                              |
|----------------------------|--------------------------------------|
| `GET /api/predict/goals`   | ETA forecasts for all active goals   |
| `GET /api/predict/learning`| learning trend                       |
| `GET /api/predict/career`  | career trend + nearest career goal   |
| `GET /api/predict/productivity` | overall activity trend          |

## Dependencies

Reads `goals`, `milestones`, `activities` from `collab.db`. No external calls,
no model downloads — fully offline.
