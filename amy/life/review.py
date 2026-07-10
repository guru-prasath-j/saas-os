"""LIFE AUTOPILOT L6 — monthly life review + integration.

Monthly vault note (09_Memory/Life Review - {month}) — the auditable
"model of you": observed vs baselines / suggested / accepted / rejected /
pruned. Idempotent per month via MemoryWriter's own eid dedup (same
"idempotent per day/meeting id" convention used everywhere else in this
codebase — capture_digest, meeting_prep, interview_debrief).
"""
from __future__ import annotations

import calendar
import datetime as _dt


def last_month(as_of: _dt.date | None = None) -> str:
    today = as_of or _dt.date.today()
    first_this = today.replace(day=1)
    last_month_end = first_this - _dt.timedelta(days=1)
    return last_month_end.strftime("%Y-%m")


def _month_bounds(month: str) -> tuple[str, str]:
    year, mon = (int(x) for x in month.split("-"))
    last_day = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last_day:02d}"


_HEADLINE_METRICS = (
    ("office_minutes", "office time", "min/day"),
    ("sleep_estimate_min", "sleep", "min/night"),
    ("gym_visits", "gym visits", "visits/day"),
)


def _observed_vs_baseline_lines(ctx, end: str, rows: list[dict]) -> list[str]:
    from .baselines import day_type_baseline

    lines = []
    for metric, label, unit in _HEADLINE_METRICS:
        judged = [r for r in rows if not r.get("grace") and r.get(metric) is not None]
        if not judged:
            continue
        avg = sum(r[metric] for r in judged) / len(judged)
        baseline = day_type_baseline(ctx, metric, "weekday", as_of=end)
        if baseline:
            lines.append(f"{label}: observed avg {avg:.0f} {unit} vs "
                         f"{baseline_weeks()}-week baseline {baseline['mean']:.0f} "
                         f"{unit} ({len(judged)} days)")
        else:
            lines.append(f"{label}: observed avg {avg:.0f} {unit} "
                         f"({len(judged)} days, no baseline yet)")
    return lines


def baseline_weeks() -> int:
    from .baselines import baseline_weeks as _bw
    return _bw()


def _approvals_in_month(ctx, start: str, end: str) -> dict:
    since = f"{start}T00:00:00"
    until = f"{end}T23:59:59"
    rows = ctx.collab.conn.execute(
        "SELECT title, status, source FROM approvals WHERE source LIKE 'life_%'"
        " AND created_at>=? AND created_at<=?", (since, until)).fetchall()
    suggested = [dict(r) for r in rows]
    accepted = [r for r in suggested if r["status"] in ("executed", "auto_executed")]
    rejected = [r for r in suggested if r["status"] == "rejected"]
    return {"suggested": suggested, "accepted": accepted, "rejected": rejected}


def _pruned_lines(ctx) -> list[str]:
    """Rule categories L9 has permanently silenced (>=2 dismissals) —
    the durable prefs counters ARE the pruning record, no new table."""
    out = []
    rows = ctx.collab.conn.execute(
        "SELECT key, value FROM prefs WHERE key LIKE 'life_opp_dismiss_%'").fetchall()
    for r in rows:
        try:
            if int(r["value"] or "0") >= 2:
                rule = r["key"][len("life_opp_dismiss_"):]
                out.append(f"opportunity rule '{rule}' silenced (2 dismissals)")
        except ValueError:
            continue
    return out


def _bulleted(items: list[str], empty: str) -> str:
    return "\n".join(f"- {i}" for i in items) if items else f"- {empty}"


def generate_month(ctx, month: str | None = None) -> dict:
    month = month or last_month()
    start, end = _month_bounds(month)

    rows = ctx.store.list_life_metrics(ctx.user_id, start, end)
    observed = _observed_vs_baseline_lines(ctx, end, rows)
    approvals = _approvals_in_month(ctx, start, end)
    pruned = _pruned_lines(ctx)

    body = "\n\n".join([
        "## Observed vs baselines",
        _bulleted(observed, "Not enough data yet."),
        f"## Suggested ({len(approvals['suggested'])})",
        _bulleted([a["title"] for a in approvals["suggested"][:20]], "None this month."),
        f"## Accepted ({len(approvals['accepted'])})",
        _bulleted([a["title"] for a in approvals["accepted"][:20]], "None this month."),
        f"## Rejected ({len(approvals['rejected'])})",
        _bulleted([a["title"] for a in approvals["rejected"][:20]], "None this month."),
        f"## Pruned ({len(pruned)})",
        _bulleted(pruned, "None."),
    ])

    from ..memory.writer import MemoryWriter
    from ..saas import tenancy
    vault = tenancy.resolve_vault_dir(ctx.user_id)
    vault.mkdir(parents=True, exist_ok=True)
    p = MemoryWriter(vault).write_atomic(
        "life review", f"Life Review - {month}", body,
        eid=f"lifereview-{ctx.user_id}-{month}", tags=["life", "review"])
    note_path = str(p) if p else "already-written"

    return {"month": month, "note": note_path,
           "suggested": len(approvals["suggested"]), "accepted": len(approvals["accepted"]),
           "rejected": len(approvals["rejected"]), "pruned": len(pruned)}
