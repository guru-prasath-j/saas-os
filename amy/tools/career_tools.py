"""Career registry tools (CAREER AUTOPILOT Part 1).

Talks to the jobspy MCP server (port 8935, "Job Search (jobspy)" connector —
see mcp_servers/jobspy_server.py) and the user's existing GitHub connector
via the same amy/connectors/mcp_call.py::call_mcp_tool() helper CONNECTOR
COMPLETION's github/plane tools use — no new connector-resolution code.

job_details is NOT a live MCP call: the jobspy server exposes exactly one
tool (search_jobs) whose results already carry the full posting (including
description) — there is nothing to re-fetch, so job_details reads back an
already-discovered row from job_postings (verified against
mcp_servers/jobspy_server.py before writing this; see docs/AGENT_PLAN.md's
CAREER AUTOPILOT pre-flight findings).

Resume text is SENSITIVE (same class as GSTIN/PAN) — stored encrypted
(AutomationStore.set_career_profile) and any LLM call that reads it back out
must route sensitive=True; this module itself makes no LLM calls.

send_hr_email is external (extras={"external": True}) — hard-pinned to tier
2 by amy/automation/executors.py's _tier_for(), exactly like github_comment/
plane_create_task: an HR email is irreversible once delivered, so
AMY_AGENT_WRITE_TIER can never soften it.
"""
from __future__ import annotations

from .registry import RISK_READ, RISK_WRITE, register_tool

_JOBSPY_SOURCE = "jobspy"
_GH_LIST_REPOS = ("list_repositories", "search_repositories")
_GH_REPO_DETAILS = ("get_repository", "repository_read")


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


# ===========================================================================
# READ tools
# ===========================================================================

@register_tool("job_search",
               "Search real job postings via the Job Search MCP connector "
               "(jobspy — indeed/linkedin/zip_recruiter/glassdoor/google/"
               "bayt/naukri). country_indeed MUST match location's country "
               "or results silently come back empty. Results are NOT saved "
               "automatically — use application_log or the job scout to "
               "persist ones worth tracking.",
               _obj({"search_term": {"type": "string"},
                     "location": {"type": "string"},
                     "site_names": {"type": "string",
                                    "description": "comma-separated, default 'indeed'"},
                     "results_wanted": {"type": "integer"},
                     "hours_old": {"type": "integer"},
                     "is_remote": {"type": "boolean"},
                     "country_indeed": {"type": "string",
                                        "description": "must match location's country, default USA"}},
                    ["search_term"]),
               RISK_READ)
def _t_job_search(ctx, args):
    from ..connectors.mcp_call import call_mcp_tool, extract_list
    call_args = {"search_term": args["search_term"]}
    for k in ("location", "site_names", "results_wanted", "hours_old",
              "is_remote", "country_indeed"):
        if args.get(k) is not None:
            call_args[k] = args[k]
    result = call_mcp_tool(ctx.user_id, ctx.store, _JOBSPY_SOURCE,
                           ("search_jobs",), call_args, target_style="none")
    jobs = extract_list(result)
    return {"jobs": jobs, "count": len(jobs)}


@register_tool("job_details",
               "Full detail of an already-discovered job posting by id "
               "(from job_search results saved via application_log, or the "
               "job scout). Local lookup — jobspy has no separate detail "
               "call; search results already carry the full description.",
               _obj({"posting_id": {"type": "string"}}, ["posting_id"]),
               RISK_READ)
def _t_job_details(ctx, args):
    from .registry import ToolError
    posting = ctx.store.get_posting(ctx.user_id, args["posting_id"])
    if posting is None:
        raise ToolError(f"no job posting {args['posting_id']!r} on file")
    return posting


@register_tool("portfolio_repo_list",
               "List the connected GitHub account's repositories (languages, "
               "stars, activity) for portfolio analysis. Omit owner for the "
               "authenticated user's own repos.",
               _obj({"owner": {"type": "string"}}),
               RISK_READ)
def _t_portfolio_repo_list(ctx, args):
    from ..connectors.mcp_call import call_mcp_tool, extract_list
    call_args = {k: v for k, v in args.items() if k == "owner" and v}
    result = call_mcp_tool(ctx.user_id, ctx.store, "github", _GH_LIST_REPOS,
                           call_args, target_style="none")
    repos = extract_list(result)
    return {"repos": repos, "count": len(repos)}


@register_tool("portfolio_repo_details",
               "One repository's full detail (README, topics, languages, "
               "activity) by owner/repo.",
               _obj({"owner": {"type": "string"}, "repo": {"type": "string"}},
                    ["repo"]),
               RISK_READ)
def _t_portfolio_repo_details(ctx, args):
    from ..connectors.mcp_call import call_mcp_tool
    call_args = {k: v for k, v in args.items() if k in ("owner", "repo")}
    return call_mcp_tool(ctx.user_id, ctx.store, "github", _GH_REPO_DETAILS,
                         call_args, target_style="owner_repo")


@register_tool("career_status",
               "Career goal + plan progress + application funnel counts "
               "(discovered/prepared/approved/sent/response/interview/"
               "offer/rejected/ghosted) for the assistant and briefing.",
               _obj({}), RISK_READ)
def _t_career_status(ctx, args):
    goal_row = ctx.collab.conn.execute(
        "SELECT * FROM goals WHERE domain='career' AND status='active'"
        " ORDER BY created_at DESC LIMIT 1").fetchone()
    profile = ctx.store.get_career_profile(ctx.user_id) or {}
    profile.pop("resume_text", None)   # never surface raw resume text here
    return {
        "profile": profile,
        "goal": dict(goal_row) if goal_row else None,
        "funnel": ctx.store.career_funnel_counts(ctx.user_id),
    }


# ===========================================================================
# WRITE tools
# ===========================================================================

@register_tool("set_career_profile",
               "Create/update the user's career profile (target role, "
               "location, deadline, skills, resume text). resume_text is "
               "stored encrypted.",
               _obj({"target_role": {"type": "string"},
                     "target_location": {"type": "string"},
                     "remote_ok": {"type": "boolean"},
                     "deadline": {"type": "string"},
                     "resume_text": {"type": "string"},
                     "skills": {"type": "array"}}),
               RISK_WRITE)
def _t_set_career_profile(ctx, args):
    ctx.store.set_career_profile(
        ctx.user_id,
        target_role=args.get("target_role"),
        target_location=args.get("target_location"),
        remote_ok=args.get("remote_ok"),
        deadline=args.get("deadline"),
        resume_text=args.get("resume_text"),
        skills=args.get("skills"))
    return {"ok": True}


@register_tool("application_log",
               "Log a new job application (creates it, status='prepared') "
               "or update an existing one's status (application_id given). "
               "Status ladder: prepared -> approved -> sent -> response -> "
               "interview -> offer, or -> rejected/ghosted at any point.",
               _obj({"application_id": {"type": "string",
                                        "description": "omit to create a new application"},
                     "posting_id": {"type": "string"},
                     "channel": {"type": "string", "description": "email|portal|third_party"},
                     "status": {"type": "string"},
                     "match_score": {"type": "number"},
                     "ats_estimate": {"type": "number"},
                     "draft": {"type": "string"},
                     "note": {"type": "string"}}),
               RISK_WRITE)
def _t_application_log(ctx, args):
    from .registry import ToolError
    if args.get("application_id"):
        ok = ctx.store.update_application_status(
            ctx.user_id, args["application_id"],
            args.get("status") or "prepared", args.get("note") or "")
        if not ok:
            raise ToolError(f"no application {args['application_id']!r} on file")
        aid = args["application_id"]
    else:
        if not args.get("posting_id"):
            raise ToolError("posting_id required to create a new application")
        aid = ctx.store.create_application(
            ctx.user_id, args["posting_id"], channel=args.get("channel") or "",
            match_score=args.get("match_score"), ats_estimate=args.get("ats_estimate"),
            draft=args.get("draft") or "", note=args.get("note") or "")
    try:
        from ..events.store import CAREER_APPLICATION_PREPARED
        ctx.events().emit(CAREER_APPLICATION_PREPARED,
                          {"application_id": aid, "posting_id": args.get("posting_id"),
                           "status": args.get("status") or "prepared"},
                          source="career_tools")
    except Exception:
        pass
    return {"id": aid}


@register_tool("plane_batch_create_tasks",
               "Create several Plane tasks at once as ONE approval item "
               "(e.g. a weekly milestone breakdown) instead of one approval "
               "per task. AGENT calls always require human approval "
               "(external send) regardless of AMY_AGENT_WRITE_TIER. "
               "Approving creates every task atomically.",
               _obj({"project_id": {"type": "string"},
                     "tasks": {"type": "array",
                              "description": "[{title, description?}, ...]"}},
                    ["tasks"]),
               RISK_WRITE, extras={"external": True})
def _t_plane_batch_create_tasks(ctx, args):
    from ..automation import executors
    return executors.execute(ctx, "plane_batch_create_tasks", args)


@register_tool("send_hr_email",
               "Send (or, without SMTP configured, prepare a copy-ready "
               "draft of) an application email to an HR/recruiter contact. "
               "AGENT calls always require human approval (external send) "
               "regardless of AMY_AGENT_WRITE_TIER — Amy never submits an "
               "application without explicit per-application approval.",
               _obj({"application_id": {"type": "string"},
                     "to_email": {"type": "string"},
                     "subject": {"type": "string"},
                     "body": {"type": "string"}},
                    ["application_id", "to_email", "subject", "body"]),
               RISK_WRITE, extras={"external": True})
def _t_send_hr_email(ctx, args):
    from ..automation import executors
    return executors.execute(ctx, "send_hr_email", args)
