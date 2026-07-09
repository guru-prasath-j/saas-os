"""GitHub + Plane registry tools (CONNECTOR COMPLETION Part 1).

Talks to the user's already-registered GitHub/Plane MCP connectors (Layer 1
registration — amy/connectors/mcp.py + amy/saas/db.py's McpConnector; see
the "github"/"plane" presets in index.html's MCP Sources panel, which point
at the official api.githubcopilot.com/mcp and mcp.plane.so servers) via
amy/connectors/mcp_call.py's shared resolve-call-log helper — the same
transport the learning-feed aggregator and amy/tools/mcp_bridge.py use.

READ tools call the connector directly in their handler (github_list_prs,
github_list_issues, github_pr_details, plane_list_tasks, plane_task_details,
meet_upcoming_meetings — the last one hits Google Calendar directly via
ctx.google_creds(), not MCP, mirroring amy/agents/calendar.py's
_google_calendar_context()).

WRITE tools (github_comment, plane_create_task, plane_update_task) follow
the existing add_subscription/set_budget convention: the registry handler
delegates to amy.automation.executors.execute() so an approved action
replays through the EXACT same code as an immediate human-actor call. They
are marked extras={"external": True} — amy/automation/executors.py's
_tier_for() hard-pins external tools to tier 2 regardless of
AMY_AGENT_WRITE_TIER, same as destructive, because a GitHub comment or
Plane task send is irreversible once delivered.
"""
from __future__ import annotations

import datetime as _dt

from .registry import RISK_READ, RISK_WRITE, register_tool


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


# Candidate remote tool names per capability, preferred first — real MCP
# servers for the same capability don't standardize naming (see module
# docstring); amy/connectors/mcp_call.py picks whichever the server actually
# advertises.
_GH_LIST_PRS = ("list_pull_requests", "search_pull_requests")
_GH_LIST_ISSUES = ("list_issues", "search_issues")
_GH_PR_DETAILS = ("get_pull_request", "pull_request_read")
_GH_COMMENT = ("add_issue_comment", "create_issue_comment")
_PLANE_LIST_TASKS = ("list_work_items", "list_issues", "get_issues")
_PLANE_TASK_DETAILS = ("get_work_item", "get_issue")
_PLANE_CREATE_TASK = ("create_work_item", "create_issue")
_PLANE_UPDATE_TASK = ("update_work_item", "update_issue")


# ===========================================================================
# READ tools
# ===========================================================================

@register_tool("github_list_prs",
               "List pull requests on the connected GitHub repo (default_target "
               "on the connector, or pass owner/repo). state: open|closed|all.",
               _obj({"owner": {"type": "string"}, "repo": {"type": "string"},
                     "state": {"type": "string", "description": "open|closed|all, default open"}}),
               RISK_READ)
def _t_github_list_prs(ctx, args):
    from ..connectors.mcp_call import call_mcp_tool
    call_args = {k: v for k, v in args.items() if k in ("owner", "repo")}
    call_args["state"] = args.get("state") or "open"
    return call_mcp_tool(ctx.user_id, ctx.store, "github", _GH_LIST_PRS, call_args)


@register_tool("github_list_issues",
               "List issues on the connected GitHub repo. state: open|closed|all.",
               _obj({"owner": {"type": "string"}, "repo": {"type": "string"},
                     "state": {"type": "string", "description": "open|closed|all, default open"}}),
               RISK_READ)
def _t_github_list_issues(ctx, args):
    from ..connectors.mcp_call import call_mcp_tool
    call_args = {k: v for k, v in args.items() if k in ("owner", "repo")}
    call_args["state"] = args.get("state") or "open"
    return call_mcp_tool(ctx.user_id, ctx.store, "github", _GH_LIST_ISSUES, call_args)


@register_tool("github_pr_details",
               "One pull request's full detail (title, body, status, review "
               "state, mergeable) by number.",
               _obj({"owner": {"type": "string"}, "repo": {"type": "string"},
                     "number": {"type": "integer"}}, ["number"]),
               RISK_READ)
def _t_github_pr_details(ctx, args):
    from ..connectors.mcp_call import call_mcp_tool
    call_args = {k: v for k, v in args.items() if k in ("owner", "repo")}
    call_args["pullNumber"] = args["number"]
    call_args["pull_number"] = args["number"]
    return call_mcp_tool(ctx.user_id, ctx.store, "github", _GH_PR_DETAILS, call_args)


@register_tool("plane_list_tasks",
               "List tasks/work-items in the connected Plane project "
               "(default_target on the connector, or pass project_id).",
               _obj({"project_id": {"type": "string"},
                     "state": {"type": "string"}}),
               RISK_READ)
def _t_plane_list_tasks(ctx, args):
    from ..connectors.mcp_call import call_mcp_tool
    call_args = {k: v for k, v in args.items() if v}
    return call_mcp_tool(ctx.user_id, ctx.store, "plane", _PLANE_LIST_TASKS,
                         call_args, target_style="single")


@register_tool("plane_task_details",
               "One Plane task's full detail by id.",
               _obj({"task_id": {"type": "string"}, "project_id": {"type": "string"}},
                    ["task_id"]),
               RISK_READ)
def _t_plane_task_details(ctx, args):
    from ..connectors.mcp_call import call_mcp_tool
    call_args = {"issue_id": args["task_id"], "work_item_id": args["task_id"]}
    if args.get("project_id"):
        call_args["project_id"] = args["project_id"]
    return call_mcp_tool(ctx.user_id, ctx.store, "plane", _PLANE_TASK_DETAILS,
                         call_args, target_style="single")


@register_tool("meet_upcoming_meetings",
               "Upcoming Google Calendar events (Meet meetings included) in "
               "the next N hours — title, start time, Meet link, attendees.",
               _obj({"hours": {"type": "integer",
                               "description": "lookahead window, default 24"}}),
               RISK_READ)
def _t_meet_upcoming(ctx, args):
    from .registry import ToolError
    creds = ctx.google_creds()
    if creds is None:
        raise ToolError("Google Calendar is not linked for this account")
    hours = int(args.get("hours") or 24)
    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff = now + _dt.timedelta(hours=max(1, hours))
    t0 = _dt.datetime.now()
    try:
        from googleapiclient.discovery import build
        svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        res = svc.events().list(
            calendarId="primary", timeMin=now.isoformat(), timeMax=cutoff.isoformat(),
            maxResults=25, singleEvents=True, orderBy="startTime").execute()
        items = res.get("items", [])
        out = []
        for e in items:
            start = e.get("start", {})
            out.append({
                "id": e.get("id"),
                "title": e.get("summary") or "(no title)",
                "start": start.get("dateTime", start.get("date", "")),
                "meet_link": e.get("hangoutLink") or "",
                "attendees": [a.get("email") for a in e.get("attendees", [])
                             if a.get("email")],
            })
        ms = int((_dt.datetime.now() - t0).total_seconds() * 1000)
        try:
            ctx.store.log_connector_call(ctx.user_id, "google_calendar",
                                         "events.list", True, ms)
        except Exception:
            pass
        return {"meetings": out}
    except Exception as exc:
        ms = int((_dt.datetime.now() - t0).total_seconds() * 1000)
        try:
            ctx.store.log_connector_call(ctx.user_id, "google_calendar",
                                         "events.list", False, ms, str(exc)[:300])
        except Exception:
            pass
        raise


# ===========================================================================
# WRITE tools — EXTERNAL, hard-pinned to tier 2 (see module docstring)
# ===========================================================================

@register_tool("github_comment",
               "Post a comment on a GitHub issue or pull request. AGENT calls "
               "always require human approval (external send) regardless of "
               "AMY_AGENT_WRITE_TIER.",
               _obj({"owner": {"type": "string"}, "repo": {"type": "string"},
                     "number": {"type": "integer"}, "body": {"type": "string"}},
                    ["number", "body"]),
               RISK_WRITE, extras={"external": True})
def _t_github_comment(ctx, args):
    from ..automation import executors
    return executors.execute(ctx, "github_comment", args)


@register_tool("plane_create_task",
               "Create a task/work-item in the connected Plane project. AGENT "
               "calls always require human approval (external send) regardless "
               "of AMY_AGENT_WRITE_TIER.",
               _obj({"project_id": {"type": "string"}, "title": {"type": "string"},
                     "description": {"type": "string"}}, ["title"]),
               RISK_WRITE, extras={"external": True})
def _t_plane_create_task(ctx, args):
    from ..automation import executors
    return executors.execute(ctx, "plane_create_task", args)


@register_tool("plane_update_task",
               "Update a task/work-item's fields (e.g. state) in Plane. AGENT "
               "calls always require human approval (external send) regardless "
               "of AMY_AGENT_WRITE_TIER.",
               _obj({"task_id": {"type": "string"}, "project_id": {"type": "string"},
                     "state": {"type": "string"}, "title": {"type": "string"}},
                    ["task_id"]),
               RISK_WRITE, extras={"external": True})
def _t_plane_update_task(ctx, args):
    from ..automation import executors
    return executors.execute(ctx, "plane_update_task", args)
