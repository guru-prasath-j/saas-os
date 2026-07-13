"""Autonomous Career Sprint (CAREER AUTOPILOT Phase C) — a weekly plan/
review loop on the existing `goals`/`milestones`/`tasks` tables, the same
structural shape as `morning_briefing` (daily) and `monthly_close`
(monthly) — a new cadence, not new infrastructure.

A "sprint" is a `goals` row created every Monday by generate_sprint() and
reviewed every Sunday by review_sprint() — domain='career_sprint'
(deliberately NOT 'career': every existing "find the career goal" query
in this codebase — _active_career_goal, job_scout_poll, portfolio_review,
career_goal_stall_check — does `WHERE domain='career' AND status='active'
ORDER BY created_at DESC LIMIT 1` expecting exactly one row; a sprint
goal sharing domain='career' would out-rank the real goal the moment
it's created and silently hijack every one of those lookups). career_meta
= {"sprint": true} (the same JSON-sidecar convention career_meta/
finance_meta already use) is set for self-description but the domain
value alone is what disambiguates sprint goals from the parent career
goal. Both are scheduled daily_at and
self-filter to the right weekday — this codebase's compute_next_run has
no native weekly schedule type (same idiom as career_graph_rebuild/
credit_score_recompute/life_wellbeing_weekly).

Tiering: the sprint-create action calls submit_action(ctx, tier=1, ...)
DIRECTLY (not through tools.invoke(actor="agent")/AGENT_GATE, which would
force _tier_for("write")'s env-driven policy, tier 2 by default). This
mirrors amy/life/habits.py::_complete()'s own hardcoded tier for
'auto_suggest_check' mode — the established way this codebase marks an
action as internal/reversible/no-external-system-touched rather than
subject to the standard write-tier config. _run_career_template's own
goal/milestone creation is UNGATED (no submit_action at all) because it's
the synchronous result of the user's own POST /api/agent/goal request;
that precedent doesn't apply to an unprompted weekly job, which needs
the record+notify step tier 1 provides.

Inputs are reused, never recomputed: skill gaps from career_graph.
top_skill_gap() (Phase B), outstanding items from Phase A's
learning_focuses/learning_feed_items. Phase A's skill-demand-driven focus
proposals (career_scout._propose_focuses_for_demand) do NOT set goal_id
on the focuses they create — only the original career-template flow
does — so "outstanding learning focus items" matches EITHER goal_id on
the active career goal OR a topic that case-insensitively matches a
current top-skill-gap entry; scoping by goal_id alone would silently
miss most of Phase A's real output.

Honesty rules: application target is null (with a reason) when there is
no trailing application history to average, never an invented fixed
number. "Skills added this week" is always available:false — career_
profile.skills is a single mutable row with no historical snapshot
anywhere in this codebase, and building one just for this diff would be
new infrastructure outside this phase's scope. No composite "profile
score"/"market readiness" number anywhere — the one forward-looking
figure is a literal count ("N skill gaps addressed out of M identified").
"""
from __future__ import annotations

import datetime as _dt

_TRAILING_WEEKS_DEFAULT = 4
_MAX_SKILL_GAPS = 2


def _trailing_weeks() -> int:
    from . import config
    try:
        return int(config._env("AMY_CAREER_SPRINT_TRAILING_WEEKS",
                                str(_TRAILING_WEEKS_DEFAULT)))
    except ValueError:
        return _TRAILING_WEEKS_DEFAULT


def _iso_week_key(d: _dt.date | None = None) -> str:
    d = d or _dt.date.today()
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def _active_career_goal_row(ctx) -> dict | None:
    row = ctx.collab.conn.execute(
        "SELECT * FROM goals WHERE domain='career' AND status='active'"
        " ORDER BY created_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def _sprint_already_generated(ctx, dedup_key: str) -> bool:
    """submit_action's tier<=1 path executes BEFORE it calls create_approval
    (whose dedup_key check is what would normally stop a repeat) — so a
    second same-week call would create a second goal even though the
    resulting approval row gets rejected as a duplicate afterward. Every
    tier<=1 caller must guard idempotency itself before calling
    submit_action; amy/life/habits.py::_complete() does the same thing
    with its own _habit_done() pre-check."""
    row = ctx.collab.conn.execute(
        "SELECT id FROM approvals WHERE dedup_key=?"
        " AND status IN ('auto_executed','executed') LIMIT 1",
        (dedup_key,)).fetchone()
    return row is not None


def _latest_sprint_goal(ctx) -> dict | None:
    row = ctx.collab.conn.execute(
        "SELECT * FROM goals WHERE domain='career_sprint'"
        " ORDER BY created_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Inputs — each pulls real stored data, never fabricates
# ---------------------------------------------------------------------------

def _unaddressed_skill_gaps(ctx, tracks: list[str]) -> dict:
    """Per active track: top _MAX_SKILL_GAPS missing skills (career_graph.
    top_skill_gap, Phase B — never recomputed) that aren't already tracked
    as an active learning-focus topic. Returns {track: {"gaps": [...],
    "total_missing": N}}."""
    from .career_graph import top_skill_gap

    existing_topics = {r["topic"].strip().lower() for r in ctx.collab.conn.execute(
        "SELECT topic FROM learning_focuses WHERE uid=? AND active=1",
        (ctx.user_id,)).fetchall()}
    out = {}
    for track in tracks:
        roadmap = top_skill_gap(ctx, track)
        missing = roadmap["missing_skills"]
        unaddressed = [e for e in missing if e["skill"].strip().lower() not in existing_topics]
        out[track] = {"gaps": unaddressed[:_MAX_SKILL_GAPS], "total_missing": len(missing)}
    return out


def _outstanding_focus_items(ctx, tracks: list[str], goal_id: str | None) -> list[dict]:
    """Active learning_focuses matching EITHER the active career goal_id
    OR a topic that case-insensitively matches a current top-skill-gap
    entry for any active track (see module docstring, finding 5) — counts
    fetched-but-not-saved/not-completed learning_feed_items per focus."""
    from .career_graph import top_skill_gap

    gap_topics: set[str] = set()
    for track in tracks:
        for e in top_skill_gap(ctx, track)["missing_skills"]:
            gap_topics.add(e["skill"].strip().lower())

    focuses = [dict(r) for r in ctx.collab.conn.execute(
        "SELECT id, topic, goal_id FROM learning_focuses WHERE uid=? AND active=1",
        (ctx.user_id,)).fetchall()]
    relevant = [f for f in focuses
               if (goal_id and f.get("goal_id") == goal_id)
               or f["topic"].strip().lower() in gap_topics]

    out = []
    for f in relevant:
        n = ctx.collab.conn.execute(
            "SELECT COUNT(*) c FROM learning_feed_items"
            " WHERE uid=? AND focus_id=? AND saved=0 AND completed_at IS NULL",
            (ctx.user_id, f["id"])).fetchone()["c"]
        if n > 0:
            out.append({"topic": f["topic"], "outstanding": n})
    return out


def _application_target(ctx) -> dict:
    """Trailing-average weekly application pace — null with a reason
    when there's no history to average, never an arbitrary fixed number
    (see module docstring, finding 6)."""
    weeks = _trailing_weeks()
    since = (_dt.datetime.now(_dt.timezone.utc)
            - _dt.timedelta(weeks=weeks)).isoformat()
    apps = [a for a in ctx.store.list_applications(ctx.user_id)
           if a.get("created_at", "") >= since]
    if not apps:
        return {"target": None,
               "reason": f"no applications in the trailing {weeks} weeks to "
                         "average a realistic pace from"}
    avg_per_week = len(apps) / weeks
    target = max(1, round(avg_per_week)) if avg_per_week > 0 else 0
    return {"target": target, "basis": f"trailing {weeks}-week average "
                                       f"({len(apps)} applications / {weeks} weeks)"}


def _maintenance_items(ctx) -> list[str]:
    """Real, checkable career_profile/event-log signals (see module
    docstring, finding 7) — never a new tracking mechanism."""
    out = []
    profile = ctx.store.get_career_profile(ctx.user_id) or {}
    if not profile.get("has_resume"):
        out.append("Upload/update your resume — application ATS estimates need it.")
    deadline = profile.get("deadline")
    if deadline:
        try:
            if _dt.date.fromisoformat(deadline[:10]) < _dt.date.today():
                out.append(f"Your career goal deadline ({deadline[:10]}) has passed — update it.")
        except ValueError:
            pass
    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=30)).isoformat()
    recent = ctx.collab.conn.execute(
        "SELECT id FROM events WHERE type='career.portfolio_analyzed' AND ts>=? LIMIT 1",
        (cutoff,)).fetchone()
    if recent is None:
        out.append("Portfolio hasn't been reviewed in over a month — run a portfolio analysis.")
    return out


# ---------------------------------------------------------------------------
# Monday — generate
# ---------------------------------------------------------------------------

def generate_sprint(ctx) -> dict:
    from .automation.executors import submit_action
    from .career_scout import _active_tracks

    goal = _active_career_goal_row(ctx)
    if goal is None:
        return {"skipped": "no active career goal"}

    profile = ctx.store.get_career_profile(ctx.user_id) or {}
    tracks = _active_tracks(profile)
    if not tracks:
        return {"skipped": "no target_role on file"}

    gaps_by_track = _unaddressed_skill_gaps(ctx, tracks)
    outstanding_focus = _outstanding_focus_items(ctx, tracks, goal["id"])
    app_target = _application_target(ctx)
    maintenance = _maintenance_items(ctx)

    milestones: list[str] = []
    tasks: list[str] = []
    total_gaps = total_addressed = 0
    for track, info in gaps_by_track.items():
        total_gaps += info["total_missing"]
        total_addressed += len(info["gaps"])
        for e in info["gaps"]:
            milestones.append(f"Make progress on '{e['skill']}' for {track}")
            tasks.append(f"Spend focused time on '{e['skill']}' ({track})")
    for item in outstanding_focus:
        milestones.append(f"Work through '{item['topic']}' learning feed "
                          f"({item['outstanding']} outstanding item(s))")
    if app_target["target"] is not None:
        milestones.append(f"Apply to {app_target['target']} more role(s) this week")
        tasks.append(f"Submit {app_target['target']} application(s)")
    else:
        tasks.append("Send your first application(s) this week to start building a pace")
    for item in maintenance:
        tasks.append(item)

    today = _dt.date.today()
    sunday = today + _dt.timedelta(days=(6 - today.weekday()))
    week_key = _iso_week_key(today)
    dedup_key = f"career_sprint_{week_key}"

    if _sprint_already_generated(ctx, dedup_key):
        return {"week": week_key, "status": "duplicate"}

    result = submit_action(
        ctx, tier=1, action_type="career_sprint_create",
        title=f"Career sprint — week of {today.isoformat()}",
        body=f"{total_addressed}/{total_gaps} skill gaps addressed this "
             f"week across {len(tracks)} track(s); "
             f"{len(outstanding_focus)} learning focus item(s) outstanding.",
        payload={"title": f"Career sprint — week of {today.isoformat()}",
                "target_date": sunday.isoformat(),
                "milestones": milestones, "tasks": tasks},
        source="career_sprint",
        dedup_key=dedup_key,
        reasoning="Weekly career sprint auto-generated from real skill-gap "
                 "(Phase B), learning-focus (Phase A), and application-pace data.",
        risk="write", affected_entity=f"goal={goal['id']}")

    try:
        from .events.factory import get_events
        from .events.store import CAREER_SPRINT_GENERATED
        get_events(ctx.user_id, ctx.collab, ctx=ctx).emit(
            CAREER_SPRINT_GENERATED,
            {"week": week_key, "status": result.get("status"),
            "skill_gaps_addressed": total_addressed, "skill_gaps_total": total_gaps},
            source="career_sprint")
    except Exception:
        pass

    return {"week": week_key, "status": result.get("status"),
           "result": result.get("result"),
           "skill_gaps_addressed": total_addressed, "skill_gaps_total": total_gaps,
           "outstanding_focus_items": len(outstanding_focus),
           "application_target": app_target, "maintenance_items": len(maintenance)}


# ---------------------------------------------------------------------------
# Sunday — review
# ---------------------------------------------------------------------------

def review_sprint(ctx) -> dict:
    from .autonomous import GoalEngine

    sprint = _latest_sprint_goal(ctx)
    if sprint is None:
        return {"skipped": "no sprint goal on file"}

    week_key = _iso_week_key(_dt.date.fromisoformat(sprint["created_at"][:10]))
    week_start = sprint["created_at"]
    week_end = _dt.datetime.now(_dt.timezone.utc).isoformat()

    engine = GoalEngine(ctx.collab)
    task_rows = engine.list_tasks(sprint["id"])
    tasks_completed = sum(1 for t in task_rows if t["done"])
    tasks_planned = len(task_rows)

    apps_this_week = [a for a in ctx.store.list_applications(ctx.user_id)
                      if week_start <= a.get("created_at", "") <= week_end]

    interviews = 0
    for a in ctx.store.list_applications(ctx.user_id):
        for entry in a.get("timeline") or []:
            if (entry.get("status") == "interview"
                    and week_start <= (entry.get("ts") or "") <= week_end):
                interviews += 1
                break

    skills_added = {"available": False,
                    "reason": "career_profile.skills has no historical "
                              "snapshot to diff against"}

    body = "\n\n".join([
        f"## Tasks: {tasks_completed}/{tasks_planned} completed",
        f"## Applications sent: {len(apps_this_week)}",
        f"## Interviews scheduled: {interviews}",
        "## Skills added: not available — no historical skill-profile snapshot",
    ])

    from .memory.writer import MemoryWriter
    from .saas import tenancy
    vault = tenancy.resolve_vault_dir(ctx.user_id)
    vault.mkdir(parents=True, exist_ok=True)
    p = MemoryWriter(vault).write_atomic(
        "career sprint review", f"Career Sprint Review - {week_key}", body,
        eid=f"career_sprint_review-{ctx.user_id}-{week_key}",
        tags=["career", "sprint", "review"])
    note_path = str(p) if p else "already-written"

    try:
        from .events.factory import get_events
        from .events.store import CAREER_SPRINT_REVIEWED
        get_events(ctx.user_id, ctx.collab, ctx=ctx).emit(
            CAREER_SPRINT_REVIEWED,
            {"week": week_key, "tasks_completed": tasks_completed,
            "tasks_planned": tasks_planned, "applications_sent": len(apps_this_week),
            "interviews_scheduled": interviews},
            source="career_sprint")
    except Exception:
        pass

    return {"week": week_key, "note": note_path,
           "tasks_completed": tasks_completed, "tasks_planned": tasks_planned,
           "applications_sent": len(apps_this_week),
           "interviews_scheduled": interviews, "skills_added": skills_added}


# ---------------------------------------------------------------------------
# Assistant tool support
# ---------------------------------------------------------------------------

def explain_progress(ctx) -> dict:
    from .autonomous import GoalEngine

    sprint = _latest_sprint_goal(ctx)
    if sprint is None:
        return {"available": False, "reason": "no sprint goal on file yet"}

    engine = GoalEngine(ctx.collab)
    task_rows = engine.list_tasks(sprint["id"])
    tasks_completed = sum(1 for t in task_rows if t["done"])
    tasks_planned = len(task_rows)
    days_remaining = None
    if sprint.get("target_date"):
        try:
            days_remaining = (_dt.date.fromisoformat(sprint["target_date"][:10])
                              - _dt.date.today()).days
        except ValueError:
            pass

    return {"available": True, "goal_id": sprint["id"], "title": sprint["title"],
           "tasks_completed": tasks_completed, "tasks_planned": tasks_planned,
           "progress_pct": engine.progress(sprint["id"]),
           "days_remaining": days_remaining}
