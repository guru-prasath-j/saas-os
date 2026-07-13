"""Opportunity Radar (CAREER AUTOPILOT Phase E) — hiring-signal
aggregation across API-backed sources ONLY. No scraping of any site that
prohibits it, and NO LinkedIn access in any form, including the user's
own session/cookies — LinkedIn actively enforces against automation,
including against individual accounts, and the realistic consequence is
the user's own account (used for real, active job applications) getting
banned. LinkedIn hiring signals are simply never produced by this
module, not stubbed as `available:false` output — there is nothing to
mark unavailable when the source was never queried.

Four real sources:
  - Hacker News "Who's Hiring" — `mcp_servers/hackernews_server.py`'s new
    `whos_hiring` tool (public Algolia HN Search API). Each hiring
    comment is a real job posting, stored in the EXISTING `job_postings`
    table (source="hn_whos_hiring") — same shape JobScoutSensor already
    uses, not a parallel table.
  - GitHub org activity — a real, if approximately company-name-to-org-
    slug-guessed, signal: does a real-match-scored company's GitHub org
    have repos pushed recently. Stored in the NEW `opportunity_signals`
    table (company-level, not a specific posting).
  - Product Hunt / Reddit — generic MCP sources via the SAME tolerant
    `call_mcp_tool` pattern `career_apply.py::_company_intel()` already
    uses for web search: honest `available: False` with no connector
    registered, real data when one is. No new connector-resolution code.

Funding/layoffs/acquisitions/engineering-blog monitoring are NEVER
attempted — there is no single clean public API for them, and this
module doesn't stub them as `available:false` noise on every result;
they simply never appear in `reasons`.

Scoring happens ONCE, at discovery time inside the scan, and is
persisted immediately (job_postings.match_score/match_factors for
posting-shaped opportunities, opportunity_signals.score/detail for
company-level ones) — mirroring career_scout.py's own "score once at
discovery, a future poll won't rescore" convention. list_opportunities()
and explain_opportunity_score() only ever read what was stored, never
recompute — same "explain never re-fabricates" rule every explain_* tool
in this codebase follows.

Discovery writes are ungated (no submit_action anywhere here) — the same
"discovery is ungated, applying is gated" split JobScoutSensor already
establishes for job_postings. Routing a discovered opportunity into an
actual application still goes through career_apply.prepare_application's
existing approval flow, no shortcut.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import logging

from .operational.sensors import Sensor

_log = logging.getLogger(__name__)

_GITHUB_ACTIVITY_SENSOR = "opportunity_github_activity"
_SCORE_APPLY_SOON = 80.0
_SCORE_REVIEW = 50.0


def _github_activity_days() -> int:
    from . import config
    try:
        return int(config._env("AMY_OPPORTUNITY_GITHUB_ACTIVITY_DAYS", "30"))
    except ValueError:
        return 30


# ---------------------------------------------------------------------------
# Scoring — deterministic, every reason traces to a real computed value
# ---------------------------------------------------------------------------

def score_opportunity(ctx, title: str, text: str, company: str, source: str,
                      base_reason: str) -> dict:
    """Deterministic weighted score, never an LLM guess. reasons only ever
    includes skill_match_*/portfolio_evidence_* when there's a real,
    nonzero backing number — never a reason implying a signal (funding,
    engineering-blog momentum) this module has no source for."""
    from .career_scout import _extract_posting_keywords

    profile = ctx.store.get_career_profile(ctx.user_id) or {}
    owned = {s.strip().lower() for s in (profile.get("skills") or []) if s.strip()}

    keywords = _extract_posting_keywords(title, text)
    reasons = [base_reason]
    skill_match_pct = None
    if keywords:
        matched = [k for k in keywords if k.lower() in owned]
        skill_match_pct = round(100.0 * len(matched) / len(keywords), 1)
        reasons.append(f"skill_match_{skill_match_pct:.0f}pct")

    showcase = ctx.store.list_portfolio_items(ctx.user_id, classification="showcase")
    kw_lower = {k.lower() for k in keywords}
    matching_projects = [
        item for item in showcase
        if kw_lower & {k.lower() for k in (item.get("matched_keywords") or [])}
    ]
    if matching_projects:
        reasons.append(f"portfolio_evidence_{len(matching_projects)}_projects")

    # Deterministic weighted formula: skill overlap dominates (up to 70
    # points), portfolio evidence adds up to 30 (6 pts/project, capped at
    # 5 projects) — not a regulation/illustrative-threshold context like
    # the Banking Risk Intelligence series, but still a fixed, documented
    # formula rather than an LLM-guessed number.
    score = 0.0
    if skill_match_pct is not None:
        score += 0.7 * skill_match_pct
    score += min(len(matching_projects), 5) * 6.0
    score = round(min(score, 100.0), 1)

    if score >= _SCORE_APPLY_SOON:
        recommended_action = "apply_soon"
    elif score >= _SCORE_REVIEW:
        recommended_action = "review"
    else:
        recommended_action = "monitor"

    return {"company": company, "score": score, "reasons": reasons, "source": source,
           "detected_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
           "recommended_action": recommended_action}


# ---------------------------------------------------------------------------
# Source: Hacker News "Who's Hiring"
# ---------------------------------------------------------------------------

def _parse_hn_comment(comment: dict) -> tuple[str, str]:
    """Best-effort title/company guess from a hiring comment's first line
    — these posts commonly use 'Company | Location | Role' or similar
    pipe/dash-separated conventions, but there's no structured field, so
    this is honestly approximate, never invented from nothing."""
    first_line = (comment.get("title") or "").strip()
    for sep in ("|", " - ", " — "):
        if sep in first_line:
            company = first_line.split(sep, 1)[0].strip()
            if company:
                return first_line, company
    return first_line, ""


def _scan_hn_whos_hiring(ctx, target_role: str) -> dict:
    from .connectors.mcp_call import ConnectorCallError, call_mcp_tool, extract_list

    try:
        result = call_mcp_tool(ctx.user_id, ctx.store, "hackernews",
                               ("whos_hiring",), {"query": target_role, "limit": 30},
                               target_style="none")
    except ConnectorCallError as exc:
        return {"available": False, "reason": str(exc)[:200]}

    comments = extract_list(result)
    discovered = 0
    for c in comments:
        url = c.get("url") or ""
        if not url:
            continue
        title, company = _parse_hn_comment(c)
        posting = {"title": title or "HN Who's Hiring posting", "company": company,
                  "url": url, "location": "", "salary": "", "is_remote": False,
                  "description": c.get("summary") or "", "source": "hn_whos_hiring"}
        from .career_scout import _extract_posting_keywords
        posting["keywords"] = _extract_posting_keywords(posting["title"], posting["description"])
        pid, is_new = ctx.store.add_posting_if_new(ctx.user_id, posting)
        if not is_new:
            continue
        discovered += 1
        scored = score_opportunity(ctx, posting["title"], posting["description"],
                                   company, "hackernews_whos_hiring", "hiring_signal_detected")
        ctx.store.set_posting_match(ctx.user_id, pid, scored["score"], scored)
        _emit_detected(ctx, scored)
    return {"available": True, "discovered": discovered}


# ---------------------------------------------------------------------------
# Source: GitHub org activity for real Phase-B-matched companies
# ---------------------------------------------------------------------------

def _slugify_company(company: str) -> str:
    return "".join(ch for ch in company.lower() if ch.isalnum())


def _scan_github_org_activity(ctx, target_role: str) -> dict:
    from . import tools
    from .career_graph import companies_matching_profile
    from .connectors.mcp_call import ConnectorCallError, find_connector_row

    if find_connector_row(ctx.user_id, "github") is None:
        return {"available": False, "reason": "no github connector"}

    companies = companies_matching_profile(ctx)["companies"]
    if not companies:
        return {"available": True, "detected": 0, "reason": "no matched companies on file yet"}

    cutoff = (_dt.datetime.now(_dt.timezone.utc)
             - _dt.timedelta(days=_github_activity_days())).isoformat()
    detected = 0
    for c in companies:
        company = c["company"]
        org = _slugify_company(company)
        if not org:
            continue
        try:
            out = tools.invoke(ctx, "portfolio_repo_list", {"owner": org}, actor="agent")
        except Exception:
            continue
        repos = out.get("repos") or []
        for r in repos:
            name = str(r.get("name") or r.get("full_name") or "").strip()
            pushed_at = str(r.get("pushed_at") or r.get("updated_at") or "")
            if not name or not pushed_at or pushed_at < cutoff:
                continue
            key = f"{company}:{name}"
            last_seen = ctx.store.sensor_seen_state(_GITHUB_ACTIVITY_SENSOR, key)
            if last_seen == pushed_at:
                continue   # already signaled this exact activity
            ctx.store.mark_sensor_seen(_GITHUB_ACTIVITY_SENSOR, key, pushed_at)
            scored = score_opportunity(
                ctx, name, str(r.get("description") or ""), company,
                "github", "github_org_activity_detected")
            sid = ctx.store.create_opportunity_signal(
                ctx.user_id, "github", company, "github_org_activity",
                {**scored, "repo": name, "pushed_at": pushed_at}, scored["score"])
            _emit_detected(ctx, scored)
            detected += 1
    return {"available": True, "detected": detected}


# ---------------------------------------------------------------------------
# Sources: Product Hunt / Reddit — generic MCP stubs, honest unavailable
# ---------------------------------------------------------------------------

def _scan_generic_mcp_source(ctx, target_role: str, source: str, candidates: tuple[str, ...],
                             signal_type: str) -> dict:
    from .connectors.mcp_call import ConnectorCallError, call_mcp_tool, extract_list

    try:
        result = call_mcp_tool(ctx.user_id, ctx.store, source, candidates,
                               {"query": target_role}, target_style="none")
    except ConnectorCallError:
        return {"available": False}   # no connector registered — honest, not fabricated

    items = extract_list(result)
    detected = 0
    for item in items[:20]:
        title = str(item.get("title") or item.get("name") or "").strip()
        if not title:
            continue
        company = str(item.get("company") or item.get("maker") or "").strip()
        text = str(item.get("description") or item.get("tagline") or item.get("body") or "")
        scored = score_opportunity(ctx, title, text, company, source,
                                   f"{signal_type}_detected")
        ctx.store.create_opportunity_signal(
            ctx.user_id, source, company, signal_type,
            {**scored, "title": title, "url": item.get("url") or ""}, scored["score"])
        _emit_detected(ctx, scored)
        detected += 1
    return {"available": True, "detected": detected}


def _scan_product_hunt(ctx, target_role: str) -> dict:
    return _scan_generic_mcp_source(ctx, target_role, "producthunt",
                                    ("search_posts", "get_posts"), "product_hunt_launch")


def _scan_reddit(ctx, target_role: str) -> dict:
    return _scan_generic_mcp_source(ctx, target_role, "reddit",
                                    ("search_posts", "search"), "reddit_hiring_post")


def _emit_detected(ctx, scored: dict) -> None:
    try:
        from .events.factory import get_events
        from .events.store import CAREER_OPPORTUNITY_DETECTED
        get_events(ctx.user_id, ctx.collab, ctx=ctx).emit(
            CAREER_OPPORTUNITY_DETECTED,
            {"source": scored["source"], "company": scored["company"], "score": scored["score"]},
            source="opportunity_radar")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------

class OpportunityRadarSensor(Sensor):
    name = "opportunity_radar"

    def __init__(self, event_store, ctx):
        super().__init__(event_store)
        self.ctx = ctx

    def poll(self) -> list[dict]:
        """One poll cycle across all real sources; same no-active-career-
        goal/no-target-role guard as JobScoutSensor.poll() (nothing to
        score against without one), and same per-source try/except
        isolation as _connector_sensor_scan — one source failing never
        blocks another."""
        goal = self.ctx.collab.conn.execute(
            "SELECT id, career_meta FROM goals WHERE domain='career' AND status='active'"
            " ORDER BY created_at DESC LIMIT 1").fetchone()
        if goal is None:
            return []

        target_role = None
        try:
            target_role = (_json.loads(goal["career_meta"] or "{}") or {}).get("target_role")
        except Exception:
            pass
        profile = self.ctx.store.get_career_profile(self.ctx.user_id) or {}
        target_role = target_role or profile.get("target_role")
        if not target_role:
            return []

        results = []
        for name, fn in (("hackernews_whos_hiring", _scan_hn_whos_hiring),
                         ("github", _scan_github_org_activity),
                         ("producthunt", _scan_product_hunt),
                         ("reddit", _scan_reddit)):
            try:
                results.append({"source": name, **fn(self.ctx, target_role)})
            except Exception as exc:
                _log.warning("opportunity_radar: %s scan failed: %s", name, exc)
                results.append({"source": name, "error": str(exc)[:200]})
        return results


def scan_opportunities(ctx) -> dict:
    """Job entry point — amy/automation/jobs.py::_opportunity_radar_scan."""
    sensor = OpportunityRadarSensor(ctx.events(), ctx)
    return {"results": sensor.poll()}


# ---------------------------------------------------------------------------
# Read surface — list_opportunities / explain_opportunity_score
# ---------------------------------------------------------------------------

def list_opportunities(ctx, source: str | None = None) -> list[dict]:
    """Reads BOTH job_postings (hn_whos_hiring) and opportunity_signals,
    normalized to the shared contract shape. STORED score/reasons only —
    never recomputed on read."""
    out = []
    postings = ctx.store.list_postings(ctx.user_id, limit=200)
    for p in postings:
        if p.get("source") != "hn_whos_hiring":
            continue
        factors = p.get("match_factors") or {}
        if not factors:
            continue
        if source and factors.get("source") != source:
            continue
        out.append({"id": f"posting:{p['id']}", "title": p.get("title"),
                   "url": p.get("url"), **factors})

    for s in ctx.store.list_opportunity_signals(ctx.user_id):
        detail = s.get("detail") or {}
        # filter on the OUTPUT contract's source field ("hackernews_
        # whos_hiring"/"github"/"producthunt"/"reddit", the prompt's own
        # enum), not the internal opportunity_signals.source storage
        # column — kept as two separate concerns even though they
        # happen to share literal values for github/producthunt/reddit.
        if source and detail.get("source") != source:
            continue
        out.append({"id": f"signal:{s['id']}", "signal_type": s.get("signal_type"),
                   "company": s.get("company"), "score": s.get("score"),
                   "source": detail.get("source", s.get("source")),
                   "reasons": detail.get("reasons", []),
                   "detected_at": s.get("detected_at"),
                   "recommended_action": detail.get("recommended_action")})

    out.sort(key=lambda o: o.get("detected_at") or "", reverse=True)
    return out


def explain_opportunity_score(ctx, opportunity_id: str) -> dict:
    """Reads the stored score/reasons verbatim — never re-scores."""
    if ":" not in opportunity_id:
        return {"available": False, "reason": "unrecognized opportunity id"}
    kind, raw_id = opportunity_id.split(":", 1)
    if kind == "posting":
        p = ctx.store.get_posting(ctx.user_id, raw_id)
        if p is None or not p.get("match_factors"):
            return {"available": False, "reason": "no stored score for this opportunity"}
        return {"available": True, **p["match_factors"]}
    if kind == "signal":
        s = ctx.store.get_opportunity_signal(ctx.user_id, raw_id)
        if s is None:
            return {"available": False, "reason": "no such opportunity signal"}
        return {"available": True, **(s.get("detail") or {})}
    return {"available": False, "reason": "unrecognized opportunity id"}
