"""Application pipeline: prepare -> approve -> send -> track (CAREER
AUTOPILOT Part 5).

prepare_application() runs the four read-only/automatic PREPARE steps
(channel recommendation, ATS estimate, company intel, tailored draft) and
lands everything as ONE approval item — the actual send is ALWAYS routed
through tools.invoke(actor="agent") regardless of who called this function,
so Amy never submits an application without an explicit per-application
approval whether a human clicked "apply" or job_scout proposed it for a
high match score (CLAUDE.md's binding constraint for this phase).

Company intel is an honest STUB when no web-search MCP connector is
registered (this codebase has none built in — verified before writing this
module) — see _company_intel()'s docstring. Never fabricates results; the
"signals, not facts" disclaimer is always attached to whatever comes back.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re

_log = logging.getLogger("amy.career_apply")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_AGENCY_KEYWORDS = ("recruiting", "staffing", "talent partner", "headhunter",
                    "recruitment agency", "talent acquisition partner")
_INTEL_FRESH_DAYS = 30
_FOLLOWUP_STALE_DAYS = 10
_GHOST_DAYS = 21

# Part 5E: statuses that block a second application at the same company.
# Everything pre-terminal counts — "accepted" too (you work there now).
_ACTIVE_APP_STATUSES = ("prepared", "approved", "sent", "response",
                        "interview", "offer", "accepted")


def _reapply_days() -> int:
    from . import config
    try:
        return int(config._env("AMY_CAREER_REAPPLY_DAYS", "60"))
    except ValueError:
        return 60


def _company_key(company: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (company or "").lower()).strip()


def duplicate_application_block(ctx, posting: dict) -> dict | None:
    """Part 5E HARD RULE: never a second application at a company that has
    one in a non-terminal state, or that rejected/ghosted us within
    AMY_CAREER_REAPPLY_DAYS (default 60). Returns {reason, application_id}
    when blocked, None when clear. Callers decide override semantics:
    prepare_application(force=True) is only reachable from the manual
    route — the agent path never passes force, so for agents this rule is
    absolute."""
    company_key = _company_key(posting.get("company") or "")
    if not company_key:
        return None
    now = _dt.datetime.now(_dt.timezone.utc)
    reapply_days = _reapply_days()
    for app in ctx.store.list_applications(ctx.user_id):
        other = ctx.store.get_posting(ctx.user_id, app["posting_id"]) or {}
        if _company_key(other.get("company") or "") != company_key:
            continue
        if app["status"] in _ACTIVE_APP_STATUSES:
            return {"reason": f"an application at {other.get('company')} is "
                              f"already active (status: {app['status']})",
                    "application_id": app["id"]}
        if app["status"] in ("rejected", "ghosted"):
            timeline = app.get("timeline") or []
            last_ts = (timeline[-1]["ts"] if timeline else None) or app.get("updated_at")
            try:
                age_days = (now - _dt.datetime.fromisoformat(last_ts)).days
            except Exception:
                continue
            if age_days < reapply_days:
                return {"reason": f"{other.get('company')} {app['status']} a "
                                  f"previous application {age_days} day(s) ago — "
                                  f"re-apply window is {reapply_days} days",
                        "application_id": app["id"]}
    return None


def _referral_paths(ctx, company: str) -> list[str]:
    """Part 5E referral check — OWN DATA ONLY, suggestions only: knowledge-
    graph nodes mentioning the company (the graph's vocabulary is
    note/email/calendar/task/goal/memory — no person type exists, so an
    email or note node that names the company IS the warm-path signal,
    surfaced with its connected nodes for context), plus vault note titles.
    Never an external lookup, never fabricated; empty when nothing is on
    file."""
    from .career_inbound import _company_token
    token = _company_token(company)
    if not token:
        return []
    hits: list[str] = []
    try:
        from pathlib import Path

        from .knowledge_graph.store import GraphStore
        g = GraphStore(str(Path(ctx.finance_path).parent / "graph.db"))
        try:
            for n in g.nodes(limit=2000):
                if token not in str(n.get("label", "")).lower():
                    continue
                neighbors = []
                for nb in g.neighbors(n["id"])[:2]:
                    node = g.get_node(nb["id"])
                    if node and node.get("label"):
                        neighbors.append(str(node["label"]))
                ctx_part = f" — linked: {', '.join(neighbors)}" if neighbors else ""
                hits.append(f"graph {n.get('type', 'node')}: "
                            f"{n['label']}{ctx_part}")
        finally:
            g.close()
    except Exception:
        pass
    try:
        from .saas import tenancy
        vault = tenancy.resolve_vault_dir(ctx.user_id)
        for i, p in enumerate(vault.rglob("*.md")):
            if i > 500:
                break
            if token in p.stem.lower():
                hits.append(f"vault note: {p.stem}")
    except Exception:
        pass
    # dedup, keep it short — this rides inside an approval body
    seen: set[str] = set()
    out = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out[:5]

_DRAFT_SYSTEM = (
    "Write a concise, professional application email for this job posting. "
    "Reference the candidate's showcase projects where genuinely relevant. "
    "Under 200 words. Never invent experience, metrics, or claims beyond "
    "what's given below. Respond with EXACTLY ONE JSON object: "
    '{"subject": "...", "body": "..."}'
)


# ---------------------------------------------------------------------------
# 1. Channel recommendation (deterministic — never fabricates contact info)
# ---------------------------------------------------------------------------

def _recommend_channel(posting: dict) -> dict:
    text = f"{posting.get('company', '')} {posting.get('description', '')}"
    emails = _EMAIL_RE.findall(text)
    if emails:
        return {"channel": "email", "to_email": emails[0],
               "reasoning": "Found a direct contact email in the posting text — "
                            "a direct HR/recruiter email historically gets a "
                            "response faster than a portal for a named contact."}
    if any(k in text.lower() for k in _AGENCY_KEYWORDS):
        return {"channel": "third_party", "to_email": None,
               "reasoning": "This looks like a staffing/recruiting agency posting, "
                            "not the hiring company directly — no private contact "
                            "was scraped; check the posting for a named recruiter "
                            "or the agency's own public contact page instead."}
    return {"channel": "portal", "to_email": None,
           "reasoning": "No direct contact email found in the posting — "
                        f"recommending the posting's own application link "
                        f"({posting.get('url', '')}), likely ATS-gated (portal "
                        "submissions are often auto-screened, so ATS keyword "
                        "coverage matters more here)."}


# ---------------------------------------------------------------------------
# 2. ATS estimate — deterministic keyword coverage, clearly labeled estimate
# ---------------------------------------------------------------------------

def _ats_estimate(resume_text: str, posting: dict) -> dict:
    """Posting-level ATS estimate. The actual coverage arithmetic lives in
    orchestrator.score_keyword_coverage() — shared with amy/jd_match.py's
    analyze_jd() (JD-level, pasted text instead of a stored posting) so
    there is exactly one keyword-coverage scorer in the codebase, not two
    subtly-different ones."""
    from .automation.orchestrator import _extract_keywords, score_keyword_coverage
    keywords = _extract_keywords([posting], top_n=15)
    out = score_keyword_coverage(resume_text, keywords)
    if out["note"] == "no extractable keywords":
        out["note"] = "no extractable keywords from this posting"
    elif out["note"] == "no resume text to score against":
        out["note"] = ("no resume on file — set one via set_career_profile "
                       "for an ATS estimate")
    return out


# ---------------------------------------------------------------------------
# 3. Company intel — honest stub without a registered web-search connector
# ---------------------------------------------------------------------------

def _company_intel(ctx, company: str) -> dict:
    """This codebase has no built-in web-search tool (verified before
    writing this module — grepped for web_search/tavily/serpapi/etc,
    nothing). Rather than fabricate hiring-process claims from LLM
    training data (explicitly forbidden for this phase), this tries a
    GENERIC 'web_search' MCP source via the same call_mcp_tool resolve-
    call-log helper GitHub/Plane/jobspy use — any web-search MCP server
    the user registers (Brave Search, Tavily, ...) under a name containing
    'web_search' just works, matching the tolerant-naming pattern already
    used everywhere else in this codebase. With none registered, this
    returns available=False honestly instead of guessing."""
    if not company:
        return {"available": False, "notes": "", "sources": []}
    cached = ctx.store.get_company_intel(ctx.user_id, company)
    if cached and cached.get("cached_at"):
        try:
            age = (_dt.datetime.now(_dt.timezone.utc)
                  - _dt.datetime.fromisoformat(cached["cached_at"])).days
            if age < _INTEL_FRESH_DAYS:
                return {"available": bool(cached.get("notes")), **cached}
        except Exception:
            pass

    from .connectors.mcp_call import ConnectorCallError, call_mcp_tool, extract_list
    notes, sources = "", []
    try:
        result = call_mcp_tool(
            ctx.user_id, ctx.store, "web_search",
            ("web_search", "search", "brave_web_search", "tavily_search"),
            {"query": f"{company} interview process hiring stages employee reviews"})
        items = extract_list(result)
        notes = " | ".join(str(i.get("title") or i.get("snippet") or "")[:200]
                           for i in items[:5])
        sources = [str(i.get("url")) for i in items[:5] if i.get("url")]
    except ConnectorCallError:
        pass   # no web-search connector registered — honest empty result, not fabricated
    except Exception as exc:
        _log.warning("career_apply: company intel search failed: %s", exc)

    ctx.store.upsert_company_intel(ctx.user_id, company, notes, sources)
    return {"available": bool(notes), "notes": notes, "sources": sources}


# ---------------------------------------------------------------------------
# 4. Draft — one sensitive=True LLM call (touches resume/skills), degrades
# ---------------------------------------------------------------------------

def _showcase_repo_names(ctx, posting: dict) -> list[str]:
    """Cheap, no-side-effect reuse of Part 3's classifier against just this
    ONE posting's keywords — deliberately does NOT call the full
    portfolio_analyze() (that proposes its own gap-project batch approval;
    running it on every single application would spam approvals)."""
    from . import tools
    from .agents.reactive import _classify_repos
    from .automation.orchestrator import _extract_keywords
    try:
        repo_out = tools.invoke(ctx, "portfolio_repo_list", {}, actor="agent")
        repos = repo_out.get("repos") or []
    except Exception:
        return []
    keywords = set(_extract_keywords([posting], top_n=15))
    if not repos or not keywords:
        return []
    showcase, _needs_work, _not_relevant = _classify_repos(repos, keywords)
    return [str(r.get("name") or r.get("full_name") or "") for r in showcase][:3]


def _draft_fallback(posting: dict, profile: dict, showcase_names: list[str],
                    target_role: str) -> dict:
    role = target_role or posting.get("title") or "this role"
    subject = f"Application: {posting.get('title', '')} at {posting.get('company', '')}"
    proj_line = (f" My work on {', '.join(showcase_names[:2])} is directly relevant."
                if showcase_names else "")
    skills = ", ".join((profile.get("skills") or [])[:5]) or "relevant areas"
    body = (f"Hello,\n\nI'm applying for {posting.get('title', '')} at "
           f"{posting.get('company', '')}. I'm targeting {role} roles and have "
           f"experience in {skills}.{proj_line}\n\n"
           "I'd welcome the chance to discuss further.\n\nBest regards")
    return {"subject": subject, "body": body}


def _draft_application(ctx, posting: dict, profile: dict, showcase_names: list[str],
                       target_role: str) -> dict:
    from .agents.reactive import _get_llm

    fallback = _draft_fallback(posting, profile, showcase_names, target_role)
    llm = _get_llm(ctx)
    if llm is None:
        return fallback
    prompt = (f"Posting: {posting.get('title')} at {posting.get('company')}\n"
             f"{(posting.get('description') or '')[:600]}\n\n"
             f"Candidate target role: {target_role}\n"
             f"Candidate skills: {', '.join(profile.get('skills') or []) or 'none on file'}\n"
             f"Showcase projects: {', '.join(showcase_names) or 'none classified yet'}")
    try:
        text, provider = llm.generate(_DRAFT_SYSTEM, prompt, sensitive=True)
        if provider == "template":
            return fallback
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return fallback
        parsed = json.loads(m.group(0))
        if parsed.get("subject") and parsed.get("body"):
            return {"subject": str(parsed["subject"])[:200],
                    "body": str(parsed["body"])[:4000]}
    except Exception as exc:
        _log.warning("career_apply: draft LLM failed, using fallback: %s", exc)
    return fallback


# ---------------------------------------------------------------------------
# 5. Orchestration — PREPARE then ONE approval
# ---------------------------------------------------------------------------

def prepare_application(ctx, posting_id: str, goal_id: str | None = None,
                        force: bool = False) -> dict:
    from . import tools

    posting = ctx.store.get_posting(ctx.user_id, posting_id)
    if posting is None:
        return {"error": f"no job posting {posting_id!r} on file"}

    # Part 5E duplicate guard — absolute for agents (they never pass force);
    # the manual route surfaces the warning + an explicit override.
    block = duplicate_application_block(ctx, posting)
    if block is not None and not force:
        return {"blocked": block["reason"],
                "duplicate_of": block["application_id"]}

    profile = ctx.store.get_career_profile(ctx.user_id) or {}
    target_role = profile.get("target_role") or posting.get("title") or ""

    channel_info = _recommend_channel(posting)
    ats = _ats_estimate(profile.get("resume_text") or "", posting)
    intel = _company_intel(ctx, posting.get("company") or "")
    showcase_names = _showcase_repo_names(ctx, posting)
    draft = _draft_application(ctx, posting, profile, showcase_names, target_role)

    app_id = ctx.store.create_application(
        ctx.user_id, posting_id, channel=channel_info["channel"],
        match_score=posting.get("match_score"), ats_estimate=ats.get("coverage_pct"),
        draft=json.dumps({**draft, "to_email": channel_info.get("to_email")}),
        note="Prepared: channel/ATS/company-intel/draft computed automatically.")

    missing_str = ", ".join(ats.get("missing") or []) or "none"
    ats_line = (f"Estimated ATS coverage: {ats['coverage_pct']}% — missing "
               f"keywords: {missing_str}." if ats.get("coverage_pct") is not None
               else f"ATS estimate unavailable ({ats.get('note', '')}).")
    intel_line = (intel["notes"][:300] if intel.get("available") else
                 "No company intel available (no web-search MCP connector "
                 "registered — add one in Account -> MCP Sources to enable this).")
    referrals = _referral_paths(ctx, posting.get("company") or "")
    referral_line = ("Possible warm paths (own data only, suggestions only): "
                     + "; ".join(referrals) if referrals
                     else "No existing connections to this company found in "
                          "the knowledge graph or vault.")
    reasoning = (
        f"Channel: {channel_info['channel']} — {channel_info['reasoning']}\n"
        f"Match score: {posting.get('match_score')}\n"
        f"{ats_line}\n"
        f"Company intel (signals, not facts): {intel_line}\n"
        f"{referral_line}\n"
        f"Draft subject: {draft['subject']}")

    ctx._extras["agent_name"] = "application_tracker_agent"
    ctx._extras["agent_reasoning"] = reasoning
    ctx._extras["agent_dedup_key"] = f"apply_{posting_id}"

    if channel_info["channel"] == "email" and channel_info.get("to_email"):
        proposal = tools.invoke(
            ctx, "send_hr_email",
            {"application_id": app_id, "to_email": channel_info["to_email"],
             "subject": draft["subject"], "body": draft["body"]},
            actor="agent")
    else:
        # portal/third-party: nothing Amy can submit on the user's behalf
        # (no scraping/portal automation) — approving just marks the
        # prep-pack ready for the human to submit manually.
        proposal = tools.invoke(
            ctx, "application_log",
            {"application_id": app_id, "status": "approved",
             "note": f"Prep-pack ready for manual {channel_info['channel']} "
                     f"submission. Draft:\n{draft['body']}"},
            actor="agent")

    try:
        from .events.store import CAREER_APPLICATION_PREPARED
        ctx.events().emit(CAREER_APPLICATION_PREPARED,
                          {"application_id": app_id, "posting_id": posting_id,
                           "channel": channel_info["channel"], "goal_id": goal_id},
                          source="application_tracker_agent")
    except Exception:
        pass

    return {"application_id": app_id, "channel": channel_info["channel"],
           "ats": ats, "company_intel": intel, "draft": draft,
           "warm_paths": referrals, "proposal": proposal}


# ---------------------------------------------------------------------------
# 6. TRACK — staleness follow-up + ghosting
# ---------------------------------------------------------------------------

def _load_draft(app: dict) -> dict:
    try:
        return json.loads(app.get("draft") or "{}")
    except Exception:
        return {}


def _has_followup_approval(ctx, app_id: str) -> bool:
    row = ctx.collab.conn.execute(
        "SELECT id FROM approvals WHERE dedup_key=?", (f"followup_{app_id}",)).fetchone()
    return row is not None


def followup_check(ctx) -> dict:
    """Job handler (every 2 days): applications sent >=10 days ago with no
    follow-up yet propose ONE follow-up email (tier 2, dedup
    followup_{application_id} — reused as the 'already followed up' check
    too, so this never proposes a second one). Applications that already
    got a follow-up and are >=21 days stale auto-mark ghosted — an internal
    status inference from already-known data, not an external send, so it
    executes directly rather than parking for approval (same precedent as
    the orchestrator's own goal bookkeeping in Part 2)."""
    from . import config, tools
    if not config.agent_enabled("application_tracker"):
        return {"skipped": "AMY_AGENT_APPLICATION_TRACKER is off"}

    apps = ctx.store.list_applications(ctx.user_id, status="sent")
    now = _dt.datetime.now(_dt.timezone.utc)
    followed_up = ghosted = 0

    for app in apps:
        timeline = app.get("timeline") or []
        last_ts = timeline[-1]["ts"] if timeline else app.get("updated_at")
        try:
            last_dt = _dt.datetime.fromisoformat(last_ts)
        except Exception:
            continue
        age_days = (now - last_dt).days
        already_followed_up = _has_followup_approval(ctx, app["id"])

        if already_followed_up:
            if age_days >= _GHOST_DAYS:
                ctx.store.update_application_status(
                    ctx.user_id, app["id"], "ghosted",
                    f"No response {age_days} days after follow-up — auto-marked ghosted.")
                try:
                    from .events.store import CAREER_APPLICATION_STATUS_CHANGED
                    ctx.events().emit(CAREER_APPLICATION_STATUS_CHANGED,
                                      {"application_id": app["id"], "status": "ghosted"},
                                      source="application_tracker_agent")
                except Exception:
                    pass
                ghosted += 1
            continue

        if age_days < _FOLLOWUP_STALE_DAYS:
            continue
        draft = _load_draft(app)
        to_email = draft.get("to_email")
        if not to_email:
            continue   # portal/third-party channel — no automated follow-up possible

        posting = ctx.store.get_posting(ctx.user_id, app["posting_id"]) or {}
        subject = f"Following up: {posting.get('title', '')} at {posting.get('company', '')}"
        body = (f"Hello,\n\nI wanted to follow up on my application for "
               f"{posting.get('title', '')} submitted on {app['created_at'][:10]}. "
               "I remain very interested and happy to provide anything further "
               "that would help.\n\nBest regards")
        ctx._extras["agent_name"] = "application_tracker_agent"
        ctx._extras["agent_reasoning"] = (
            f"No response in {age_days} days since the last update — "
            "proposing ONE follow-up email.")
        ctx._extras["agent_dedup_key"] = f"followup_{app['id']}"
        tools.invoke(ctx, "send_hr_email",
                     {"application_id": app["id"], "to_email": to_email,
                      "subject": subject, "body": body}, actor="agent")
        followed_up += 1

    return {"checked": len(apps), "followed_up": followed_up, "ghosted": ghosted}
