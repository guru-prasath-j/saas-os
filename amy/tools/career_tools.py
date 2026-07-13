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
    from ..connectors.mcp_call import (ConnectorCallError, call_mcp_tool,
                                       extract_list, find_connector_row)
    call_args = {k: v for k, v in args.items() if k == "owner" and v}
    try:
        result = call_mcp_tool(ctx.user_id, ctx.store, "github",
                               ("list_repositories",), call_args,
                               target_style="none")
    except ConnectorCallError:
        # The OFFICIAL GitHub MCP server advertises no plain list tool —
        # only search_repositories, which hard-requires a query (found
        # live: the old candidate order picked it and errored on the
        # missing parameter). Build the query from the explicit owner or
        # the connector's default_target ("owner/repo" -> owner).
        owner = (args.get("owner") or "").strip()
        if not owner:
            row = find_connector_row(ctx.user_id, "github")
            target = (getattr(row, "default_target", "") or "").strip() if row else ""
            owner = target.split("/", 1)[0] if target else ""
        if not owner:
            raise
        result = call_mcp_tool(ctx.user_id, ctx.store, "github",
                               ("search_repositories",),
                               {"query": f"user:{owner}"}, target_style="none")
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


@register_tool("skill_demand_report",
               "Market-demand report over recently discovered job_postings "
               "keywords, per active target track (career_profile."
               "target_role). Read-only in registry terms — computing the "
               "report is a read; it may internally propose Learning Feed "
               "focuses for frequently-demanded missing skills, which is "
               "what's actually gated (create_learning_focus, tier-2 by "
               "default). Omit track to get all active tracks' reports.",
               _obj({"track": {"type": "string"},
                    "propose": {"type": "boolean"}}),
               RISK_READ)
def _t_skill_demand_report(ctx, args):
    from ..career_scout import skill_demand_report, skill_demand_reports
    propose = args.get("propose")
    propose = True if propose is None else bool(propose)
    if args.get("track"):
        return skill_demand_report(ctx, args["track"], propose=propose)
    return {"tracks": skill_demand_reports(ctx, propose=propose)}


@register_tool("rebuild_career_graph",
               "Rebuild the Career Intelligence Graph (skill/company/"
               "project/target_role nodes in the shared knowledge graph) "
               "from job_postings, applications, career_profile, and a "
               "live GitHub portfolio classify pass. Runs weekly via the "
               "career_graph_rebuild job — call this to refresh on demand "
               "instead of waiting. Read-only against every OTHER table "
               "(only writes to the graph store).",
               _obj({}), RISK_READ)
def _t_rebuild_career_graph(ctx, args):
    from ..career_graph import rebuild_career_graph
    return rebuild_career_graph(ctx)


@register_tool("top_skill_gap",
               "Skill-gap roadmap for a target role/track, ordered by "
               "demand frequency across matched postings (reuses "
               "skill_demand_report — never recomputes). No salary/"
               "compensation numbers — no such data exists in this system.",
               _obj({"target_role": {"type": "string"}}, ["target_role"]),
               RISK_READ)
def _t_top_skill_gap(ctx, args):
    from ..career_graph import top_skill_gap
    return top_skill_gap(ctx, args["target_role"])


@register_tool("companies_matching_profile",
               "Companies whose postings repeatedly score well against "
               "the candidate's profile, using the EXISTING stored "
               "job_postings.match_score (never recomputed). Unscored "
               "postings are excluded from a company's average, never "
               "treated as a 0.",
               _obj({"min_avg_score": {"type": "number"}}), RISK_READ)
def _t_companies_matching_profile(ctx, args):
    from ..career_graph import companies_matching_profile
    min_score = args.get("min_avg_score")
    return companies_matching_profile(ctx, min_avg_score=70.0 if min_score is None
                                      else float(min_score))


@register_tool("explain_sprint_progress",
               "Current weekly career sprint's progress (CAREER AUTOPILOT "
               "Phase C): tasks completed vs. planned, overall progress "
               "percent, and days remaining until the sprint's target "
               "date. Honest available:false if no sprint has been "
               "generated yet (needs an active career goal).",
               _obj({}), RISK_READ)
def _t_explain_sprint_progress(ctx, args):
    from ..career_sprint import explain_progress
    return explain_progress(ctx)


@register_tool("why_rejected",
               "Cross-references a rejected application's posting "
               "keywords against your CURRENT skill profile for a "
               "possible skill-gap explanation. Never claims certainty — "
               "confidence is 'none' when the signal is too weak to say "
               "anything, capped at 'low'/'moderate' otherwise (a keyword "
               "gap correlating with a rejection is never proof of why a "
               "human rejected it).",
               _obj({"application_id": {"type": "string"}}, ["application_id"]),
               RISK_READ)
def _t_why_rejected(ctx, args):
    from ..career_graph import why_rejected
    return why_rejected(ctx, args["application_id"])


@register_tool("list_portfolio_items",
               "Persisted portfolio classification (CAREER AUTOPILOT "
               "Phase D) — SHOWCASE/NEEDS_WORK/NOT_RELEVANT per repo, "
               "from the last portfolio analysis pass. Omit "
               "classification for all items.",
               _obj({"classification": {"type": "string"}}), RISK_READ)
def _t_list_portfolio_items(ctx, args):
    return {"items": ctx.store.list_portfolio_items(
        ctx.user_id, classification=args.get("classification"))}


@register_tool("list_resume_versions",
               "Metadata for stored resume versions (CAREER AUTOPILOT "
               "Phase D) — id/label/target_track/created_at/char count "
               "only, never the decrypted content.",
               _obj({}), RISK_READ)
def _t_list_resume_versions(ctx, args):
    return {"versions": ctx.store.list_resume_versions(ctx.user_id)}


@register_tool("interview_patterns",
               "Pattern analysis over your OWN logged interviews (CAREER "
               "AUTOPILOT Phase F) — recurring weakness tags, outcome "
               "counts per round type, and skill gaps ONLY where a "
               "weakness tag matched a real Phase B skill-graph node. "
               "Retrospective aggregation, never a forecast.",
               _obj({}), RISK_READ)
def _t_interview_patterns(ctx, args):
    from ..interview_memory import interview_patterns
    return interview_patterns(ctx)


@register_tool("interview_weakness_report",
               "Plain-language summary of your most common interview "
               "weakness (CAREER AUTOPILOT Phase F) — reuses interview_"
               "patterns, never recomputes.",
               _obj({}), RISK_READ)
def _t_interview_weakness_report(ctx, args):
    from ..interview_memory import interview_weakness_report
    return interview_weakness_report(ctx)


@register_tool("list_companies",
               "Discovered/tracked companies (Company Discovery "
               "extension) — free-sources-only ATS/Himalayas/TheirStack/"
               "GitHub fan-out. Filterable by city/confidence/is_target. "
               "confidence='high' means 2+ sources or a role_title+"
               "keyword match on one source; 'verify' otherwise.",
               _obj({"city": {"type": "string"}, "confidence": {"type": "string"},
                     "is_target": {"type": "boolean"}}), RISK_READ)
def _t_list_companies(ctx, args):
    from ..company_discovery import list_companies
    return {"companies": list_companies(ctx, city=args.get("city"),
                                        confidence=args.get("confidence"),
                                        is_target=args.get("is_target"))}


@register_tool("recent_fast_track_postings",
               "Postings discovered via direct Greenhouse/Lever/Ashby "
               "ATS polling (Company Discovery extension) — the fastest "
               "source in this system, since it's the company's own "
               "feed, no aggregator lag. Discovery only — these don't "
               "auto-enter the application pipeline.",
               _obj({"limit": {"type": "integer"}}), RISK_READ)
def _t_recent_fast_track_postings(ctx, args):
    from ..company_discovery import recent_fast_track_postings
    limit = args.get("limit")
    return {"postings": recent_fast_track_postings(ctx, limit=int(limit) if limit else 20)}


@register_tool("list_opportunities",
               "Discovered hiring signals (CAREER AUTOPILOT Phase E) — "
               "Hacker News 'Who's Hiring' postings + GitHub org "
               "activity/Product Hunt/Reddit company-level signals for "
               "real Phase-B-matched companies. STORED score/reasons "
               "only, scored once at discovery, never recomputed here. "
               "No LinkedIn signals exist in this system, by design. "
               "Omit source for all.",
               _obj({"source": {"type": "string"}}), RISK_READ)
def _t_list_opportunities(ctx, args):
    from ..opportunity_radar import list_opportunities
    return {"opportunities": list_opportunities(ctx, source=args.get("source"))}


@register_tool("explain_opportunity_score",
               "Explain a discovered opportunity's stored score/reasons "
               "(CAREER AUTOPILOT Phase E) — reads the stored value "
               "verbatim, never re-scores. opportunity_id comes from "
               "list_opportunities (e.g. 'posting:abc123' or "
               "'signal:def456').",
               _obj({"opportunity_id": {"type": "string"}}, ["opportunity_id"]),
               RISK_READ)
def _t_explain_opportunity_score(ctx, args):
    from ..opportunity_radar import explain_opportunity_score
    return explain_opportunity_score(ctx, args["opportunity_id"])


@register_tool("resume_performance",
               "Per-resume-version outcome counts (applications/"
               "interviews/offers), joined from applications.resume_"
               "version_id. Versions used by fewer than 3 applications "
               "are marked confidence:'insufficient_data' rather than "
               "given a rate — never implies statistical confidence "
               "that isn't there.",
               _obj({}), RISK_READ)
def _t_resume_performance(ctx, args):
    from ..career_resume import resume_performance
    return resume_performance(ctx)


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


@register_tool("propose_portfolio_update",
               "Propose a refreshed description/bullets for one persisted "
               "portfolio repo (CAREER AUTOPILOT Phase D) — ALWAYS parks "
               "a tier-2 approval regardless of actor (the function calls "
               "submit_action directly), never auto-applies; nothing is "
               "written back to GitHub, only Amy's own local suggestion.",
               _obj({"repo_name": {"type": "string"},
                     "why": {"type": "string"},
                     "bullets": {"type": "array"}},
                    ["repo_name", "why", "bullets"]),
               RISK_WRITE)
def _t_propose_portfolio_update(ctx, args):
    from ..career_portfolio import propose_portfolio_update
    return propose_portfolio_update(ctx, args["repo_name"], args["why"],
                                    args.get("bullets") or [], source="assistant")


@register_tool("generate_resume_version",
               "Generate a track-specific resume draft from career_"
               "profile skills + persisted showcase portfolio bullets + "
               "real skill-demand ordering (CAREER AUTOPILOT Phase D) — "
               "ALWAYS parks a tier-2 approval regardless of actor, never "
               "auto-saves a version. Never inserts a skill not already "
               "on the candidate's profile.",
               _obj({"target_track": {"type": "string"},
                     "label": {"type": "string"}},
                    ["target_track"]),
               RISK_WRITE)
def _t_generate_resume_version(ctx, args):
    from ..career_resume import generate_resume_version
    return generate_resume_version(ctx, args["target_track"], label=args.get("label"))


@register_tool("log_interview",
               "Log an interview you had — a manual journal entry, NOT a "
               "detection system (CAREER AUTOPILOT Phase F). Auto-"
               "executed + notified (tier 1, the internal submit_action "
               "call always fixes this regardless of actor) — this is "
               "your own self-report, not an external action. If "
               "application_id is given, company is derived from the "
               "linked posting, not independently trusted.",
               _obj({"application_id": {"type": "string"},
                     "company": {"type": "string"},
                     "round_type": {"type": "string",
                                   "description": "phone_screen|technical|"
                                                  "system_design|behavioral|onsite|other"},
                     "questions": {"type": "array"},
                     "self_assessed_outcome": {"type": "string",
                                              "description": "strong|ok|weak"},
                     "weakness_tags": {"type": "array"},
                     "notes": {"type": "string"}}),
               RISK_WRITE)
def _t_log_interview(ctx, args):
    from ..interview_memory import log_interview
    from .registry import ToolError
    try:
        return log_interview(
            ctx, application_id=args.get("application_id"),
            company=args.get("company", ""), round_type=args.get("round_type", "other"),
            questions=args.get("questions"),
            self_assessed_outcome=args.get("self_assessed_outcome", "ok"),
            weakness_tags=args.get("weakness_tags"), notes=args.get("notes", ""))
    except ValueError as exc:
        raise ToolError(str(exc))


@register_tool("log_interview_from_chat",
               "Dictate an interview debrief conversationally — the "
               "assistant structures it into the log_interview schema "
               "(CAREER AUTOPILOT Phase F). NEVER invents a question or "
               "weakness beyond what you actually said; degrades to a "
               "verbatim notes-only entry if structuring fails. Same "
               "tier-1 auto-with-notification as log_interview.",
               _obj({"company": {"type": "string"},
                     "description": {"type": "string",
                                    "description": "freeform debrief text"}},
                    ["company", "description"]),
               RISK_WRITE)
def _t_log_interview_from_chat(ctx, args):
    from ..interview_memory import log_interview_from_chat
    return log_interview_from_chat(ctx, args["company"], args["description"])


@register_tool("set_company_target",
               "Mark/unmark a discovered company as a fast-track target "
               "(Company Discovery extension) — only is_target=true "
               "companies get polled by the hourly ATS fast-track job. "
               "The user's own curation over their own local data, no "
               "external effect — executes directly, same as updating "
               "an application's status.",
               _obj({"company_id": {"type": "string"},
                     "is_target": {"type": "boolean"}},
                    ["company_id", "is_target"]),
               RISK_WRITE)
def _t_set_company_target(ctx, args):
    from ..company_discovery import set_company_target
    from .registry import ToolError
    ok = set_company_target(ctx, args["company_id"], bool(args["is_target"]))
    if not ok:
        raise ToolError(f"no company {args['company_id']!r} on file")
    return {"ok": True}


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
