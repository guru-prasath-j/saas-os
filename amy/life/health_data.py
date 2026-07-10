"""LIFE AUTOPILOT L8 — wearable health-data stub.

Tries a generic 'health_data' MCP source (Health-Connect/Google-Fit-
shaped) via call_mcp_tool's tolerant candidate-naming — the exact same
idiom career_apply.py's company-intel stub uses for a missing
web_search connector. Honestly returns available=False, nothing
fabricated, when no such connector is registered (the common path
today — this repo has no built-in health/wearable connector). When one
IS registered, sleep upgrades to device data (aggregator.py sets
sleep_provenance='device') and steps/workouts become real, checkable
habit_links signal types.
"""
from __future__ import annotations

_HEALTH_SOURCE = "health_data"
_SLEEP_CANDIDATES = ("get_sleep_data", "sleep_sessions", "get_sleep", "getSleepData")
_ACTIVITY_CANDIDATES = ("get_steps", "step_count", "get_daily_steps", "get_activity")


def fetch_device_day(ctx, date: str) -> dict:
    """{available: False} with nothing else populated when no
    health_data-shaped connector is registered or the call fails — never
    a guessed value. {available: True, sleep_window_start, sleep_window_end,
    sleep_estimate_min} when a connector returns usable sleep data."""
    try:
        from ..connectors.mcp_call import call_mcp_tool
        result = call_mcp_tool(ctx.user_id, ctx.store, _HEALTH_SOURCE,
                               _SLEEP_CANDIDATES, {"date": date}, target_style="none")
    except Exception:
        return {"available": False}
    data = (result or {}).get("result")
    if not isinstance(data, dict):
        return {"available": False}
    start = data.get("sleep_start") or data.get("start")
    end = data.get("sleep_end") or data.get("end")
    minutes = data.get("duration_min") or data.get("minutes")
    if not (start and end):
        return {"available": False}
    return {"available": True, "sleep_window_start": str(start)[:5],
           "sleep_window_end": str(end)[:5],
           "sleep_estimate_min": float(minutes) if minutes else None}


def fetch_device_activity(ctx, date: str) -> dict:
    """{available: False} honestly with no connector; {available: True,
    steps, workouts} otherwise — the steps/workouts habit_links signal
    types read from this."""
    try:
        from ..connectors.mcp_call import call_mcp_tool
        result = call_mcp_tool(ctx.user_id, ctx.store, _HEALTH_SOURCE,
                               _ACTIVITY_CANDIDATES, {"date": date}, target_style="none")
    except Exception:
        return {"available": False}
    data = (result or {}).get("result")
    if not isinstance(data, dict):
        return {"available": False}
    steps = data.get("steps") or data.get("step_count")
    workouts = data.get("workouts") or data.get("workout_count")
    if steps is None and workouts is None:
        return {"available": False}
    return {"available": True, "steps": steps, "workouts": workouts}
