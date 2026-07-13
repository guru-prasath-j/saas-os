"""Company Discovery + ATS Fast-Track Posting Detection (extends CAREER
AUTOPILOT Phase E / Opportunity Radar) — FREE SOURCES ONLY. No paid API,
no paid Apify actor, nothing that charges per request even fractionally.
A source that needs a credit card or a paid plan beyond a trial/token
allowance is `available:false` here, full stop — never a workaround.

Explicitly, permanently excluded (no free API exists, or the source is
banned outright) — these never appear anywhere in this module's output,
not even as `available:false` noise, matching this codebase's "absent,
not padded" convention (see amy/opportunity_radar.py's funding/layoff
handling): Naukri, NaukriGulf, Bayt, GulfTalent (real Apify actors
exist, all paid-per-result), Fantastic.jobs / Career Site Job Listing
API (paid-per-event, including its bundled LinkedIn enrichment — that
enrichment path doesn't exist in this build), Crunchbase API (paid),
LinkedIn Jobs in ANY form — paid or free scraper, login-based or not,
same hard ban as amy/opportunity_radar.py's — and Clutch/TechBehemoths/
GoodFirms/Tracxn/YourStory/MAGNiTT/Cutshort/Wellfound/Instahyre (no free
API for any of them). This is an acknowledged coverage gap (weaker
Indian/Gulf job-board coverage, no LinkedIn company enrichment), not
something silently worked around.

Three real, free source tiers:
  Tier 1 (does the heavy lifting) — company ATS platforms' own public
  JSON job feeds: Greenhouse (boards-api.greenhouse.io), Lever
  (api.lever.co), Ashby (api.ashbyhq.com) — the company publishing its
  own postings, no key, no aggregator lag. Fetched via stdlib
  `urllib.request` directly (same precedent as amy/obligations/
  zakat.py::_fetch_spot_usd() for a free, keyless, official public API —
  not a new MCP server, not the `requests` library as a new main-app
  dependency).
  Tier 2 — free registered MCP sources (Himalayas: fully free, no key;
  TheirStack: genuine free tier, 200 credits/month — this module never
  upgrades it, and degrades to available:false for the rest of a run the
  moment its free credits look exhausted, rather than erroring the whole
  job). Same generic-MCP-source/honest-unavailable pattern amy/
  career_apply.py::_company_intel() and amy/opportunity_radar.py's
  Product Hunt/Reddit legs already established — registering Himalayas/
  TheirStack is a user action via the EXISTING POST /api/mcp/connectors
  route (amy/saas/routers/mcp_connectors.py), never something this
  module inserts on the user's behalf.
  GitHub — free, keyless, already used elsewhere (GitHubSensor,
  portfolio analysis). Repo search has no location: qualifier (that's a
  user/org search qualifier), so location-targeted discovery is a
  bounded two-step: search_repositories by keyword, then ONE search_
  users location check per candidate org — degrades per-candidate, never
  blocks the rest.

detect_ats_platform() is used BOTH here and by amy/automation/store.py::
add_posting_if_new() — every posting-discovery path in this codebase
(JobScoutSensor, opportunity_radar's HN scan, this module's own polls)
funnels through that one method, so hooking URL-pattern detection there
covers all of them for free, opportunistically, without touching each
call site. A URL that doesn't match a known ATS pattern gets
ats_platform left None — never guessed.

Discovery and fast-track detection stay tier-0/read-only into the
existing pipeline — a detected posting never auto-enters application;
that still goes through career_scout.py's match-scoring + career_apply.
py's approval gating, no shortcut.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import re
import urllib.request

_ATS_PATTERNS = (
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)", re.IGNORECASE)),
    ("lever", re.compile(r"jobs\.lever\.co/([^/?#]+)", re.IGNORECASE)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)", re.IGNORECASE)),
)

_DEFAULT_DISCOVERY_CITIES = ("Bengaluru", "Dubai")   # illustrative defaults
# matching the prompt's own named example — used ONLY when the user has
# no target_location on file; never presumed for a user who does.

_LINKEDIN_COMPANY_RE = re.compile(r"linkedin\.com/company/([\w\-]+)", re.IGNORECASE)


def detect_ats_platform(url: str) -> tuple[str, str] | None:
    """(platform, slug) from a real Greenhouse/Lever/Ashby posting URL, or
    None (never a guess) if it doesn't match a known pattern."""
    if not url:
        return None
    for platform, pattern in _ATS_PATTERNS:
        m = pattern.search(url)
        if m:
            return platform, m.group(1)
    return None


# ---------------------------------------------------------------------------
# Tier 1 — direct ATS polling
# ---------------------------------------------------------------------------

def _fetch_json(url: str):
    """Same stdlib urllib.request pattern as amy/obligations/zakat.py's
    _fetch_spot_usd() — a free, keyless, official public API. Never
    raises; None on any failure (timeout, HTTP error, bad JSON)."""
    req = urllib.request.Request(url, headers={"User-Agent": "amy-personalos"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return _json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def poll_greenhouse(slug: str) -> list[dict]:
    data = _fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if not isinstance(data, dict):
        return []
    out = []
    for j in data.get("jobs") or []:
        url = j.get("absolute_url") or ""
        if not url:
            continue
        out.append({"title": j.get("title") or "",
                    "url": url,
                    "location": (j.get("location") or {}).get("name") or "",
                    "description": ""})
    return out


def poll_lever(slug: str) -> list[dict]:
    data = _fetch_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        return []
    out = []
    for j in data:
        url = j.get("hostedUrl") or ""
        if not url:
            continue
        out.append({"title": j.get("text") or "",
                    "url": url,
                    "location": (j.get("categories") or {}).get("location") or "",
                    "description": ""})
    return out


def poll_ashby(slug: str) -> list[dict]:
    data = _fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not isinstance(data, dict):
        return []
    out = []
    for j in data.get("jobs") or []:
        url = j.get("jobUrl") or j.get("applyUrl") or ""
        if not url:
            continue
        out.append({"title": j.get("title") or "",
                    "url": url,
                    "location": str(j.get("location") or j.get("locationName") or ""),
                    "description": ""})
    return out


_POLL_FUNCS = {"greenhouse": poll_greenhouse, "lever": poll_lever, "ashby": poll_ashby}


def _insert_ats_posting(ctx, company: str, platform: str, r: dict) -> tuple[str, bool]:
    from .career_scout import _extract_posting_keywords

    posting = {"title": r["title"], "company": company, "url": r["url"],
              "location": r.get("location", ""), "salary": "", "is_remote": False,
              "description": r.get("description", ""), "source": f"ats_{platform}"}
    posting["keywords"] = _extract_posting_keywords(posting["title"], posting["description"])
    return ctx.store.add_posting_if_new(ctx.user_id, posting)


def _emit_fast_detected(ctx, posting_id: str, company: str, platform: str) -> None:
    try:
        from .events.factory import get_events
        from .events.store import CAREER_JOB_POSTING_DETECTED_FAST
        get_events(ctx.user_id, ctx.collab, ctx=ctx).emit(
            CAREER_JOB_POSTING_DETECTED_FAST,
            {"posting_id": posting_id, "company": company, "platform": platform},
            source="company_discovery")
    except Exception:
        pass


def ats_fast_poll(ctx) -> dict:
    """Hourly job: only company_intel rows with ats_platform set AND
    is_target=1 — the user's own curated targets, not every company
    company_discovery_scan has ever seen."""
    companies = ctx.store.list_ats_known_companies(ctx.user_id, target_only=True)
    if not companies:
        return {"skipped": "no ATS target companies on file"}

    companies_polled = discovered = 0
    for c in companies:
        platform, slug = c.get("ats_platform"), c.get("ats_company_slug")
        poll_fn = _POLL_FUNCS.get(platform)
        if poll_fn is None or not slug:
            continue
        results = poll_fn(slug)
        companies_polled += 1
        for r in results:
            if not r.get("title") or not r.get("url"):
                continue
            pid, is_new = _insert_ats_posting(ctx, c["company"], platform, r)
            if is_new:
                discovered += 1
                _emit_fast_detected(ctx, pid, c["company"], platform)
        ctx.store.set_company_last_ats_poll(
            ctx.user_id, c["company"], _dt.datetime.now(_dt.timezone.utc).isoformat())
    return {"companies_polled": companies_polled, "discovered": discovered}


# ---------------------------------------------------------------------------
# Weekly broad discovery
# ---------------------------------------------------------------------------

def _discovery_cities(profile: dict) -> list[str]:
    loc = (profile.get("target_location") or "").strip()
    if not loc:
        return list(_DEFAULT_DISCOVERY_CITIES)
    return [c.strip() for c in re.split(r"[,/]| and ", loc) if c.strip()]


def _tier1_leg(ctx) -> dict[str, dict]:
    """Broader than ats_fast_poll — every ats_platform-known company,
    regardless of is_target (weekly refresh vs. the hourly poller's
    narrower target-only scope)."""
    hits: dict[str, dict] = {}
    for c in ctx.store.list_ats_known_companies(ctx.user_id, target_only=False):
        platform, slug = c.get("ats_platform"), c.get("ats_company_slug")
        poll_fn = _POLL_FUNCS.get(platform)
        if poll_fn is None or not slug:
            continue
        for r in poll_fn(slug):
            if not r.get("title") or not r.get("url"):
                continue
            _insert_ats_posting(ctx, c["company"], platform, r)
        ctx.store.set_company_last_ats_poll(
            ctx.user_id, c["company"], _dt.datetime.now(_dt.timezone.utc).isoformat())
        hits[c["company"]] = {"sources": {"ats"}, "matched_via": {"keyword"}}
    return hits


def _record_hit(hits: dict, company: str, source: str, matched_via: str,
                city: str = "") -> None:
    company = (company or "").strip()
    if not company:
        return
    entry = hits.setdefault(company, {"sources": set(), "matched_via": set(), "city": ""})
    entry["sources"].add(source)
    entry["matched_via"].add(matched_via)
    if city:
        entry["city"] = city


def _tier2_leg(ctx, target_role: str, target_location: str) -> tuple[dict, dict]:
    from .connectors.mcp_call import ConnectorCallError, call_mcp_tool, extract_list

    hits: dict[str, dict] = {}
    availability = {}

    try:
        result = call_mcp_tool(ctx.user_id, ctx.store, "himalayas",
                               ("search_jobs", "search"), {"query": target_role},
                               target_style="none")
        for item in extract_list(result)[:20]:
            title = str(item.get("title") or "")
            matched_via = "role_title" if target_role.lower() in title.lower() else "keyword"
            _record_hit(hits, item.get("company") or "", "himalayas", matched_via)
        availability["himalayas"] = True
    except ConnectorCallError:
        availability["himalayas"] = False

    try:
        result = call_mcp_tool(ctx.user_id, ctx.store, "theirstack",
                               ("search_jobs", "search"),
                               {"query": target_role, "location": target_location},
                               target_style="none")
        for item in extract_list(result)[:20]:
            title = str(item.get("title") or "")
            matched_via = "role_title" if target_role.lower() in title.lower() else "keyword"
            _record_hit(hits, item.get("company") or "", "theirstack", matched_via)
        availability["theirstack"] = True
    except ConnectorCallError:
        # Covers both "not registered" and a credit-exhaustion-shaped
        # failure (429/quota wording surfaces inside the same exception
        # from call_mcp_tool's remote-error path) — either way, this leg
        # degrades honestly for the rest of THIS run; the job still
        # completes and the other legs are unaffected.
        availability["theirstack"] = False

    return hits, availability


def _github_leg(ctx, cities: list[str], keywords: list[str]) -> dict:
    """Bounded two-step (finding 6): search_repositories by keyword, then
    ONE search_users location check per candidate org — capped low,
    each candidate independently try/excepted so a server without
    search_users just leaves that company's location unconfirmed rather
    than blocking the rest."""
    from .connectors.mcp_call import ConnectorCallError, call_mcp_tool, extract_list

    hits: dict[str, dict] = {}
    for city in cities[:2]:
        for kw in keywords[:2]:
            try:
                result = call_mcp_tool(ctx.user_id, ctx.store, "github",
                                       ("search_repositories",), {"query": kw},
                                       target_style="none")
            except ConnectorCallError:
                continue
            for repo in extract_list(result)[:5]:
                owner = repo.get("owner") or {}
                login = owner.get("login") if isinstance(owner, dict) else None
                if not login or owner.get("type") != "Organization":
                    continue
                try:
                    loc_result = call_mcp_tool(
                        ctx.user_id, ctx.store, "github",
                        ("search_users", "search_organizations"),
                        {"query": f"user:{login} location:{city}"}, target_style="none")
                except ConnectorCallError:
                    continue
                if extract_list(loc_result):
                    _record_hit(hits, login, "github", "keyword", city=city)
    return hits


def _merge_hits(*hit_dicts: dict) -> dict:
    merged: dict[str, dict] = {}
    for hits in hit_dicts:
        for company, info in hits.items():
            entry = merged.setdefault(company, {"sources": set(), "matched_via": set(), "city": ""})
            entry["sources"] |= info.get("sources", set())
            entry["matched_via"] |= info.get("matched_via", set())
            if info.get("city"):
                entry["city"] = info["city"]
    return merged


def _persist_hits(ctx, merged: dict) -> int:
    """confidence = 'high' when sources_count>=2 OR matched_via=='both'
    (both role_title AND keyword matched on even one source), else
    'verify' — the prompt's exact rule."""
    n = 0
    for company, info in merged.items():
        matched_via_set = info["matched_via"]
        matched_via = "both" if len(matched_via_set) > 1 else next(iter(matched_via_set), "keyword")
        confidence = "high" if (len(info["sources"]) >= 2 or matched_via == "both") else "verify"
        ctx.store._upsert_company_discovery(
            ctx.user_id, company, city=info.get("city", ""), matched_via=matched_via,
            confidence=confidence, relevance_tags=sorted(info["sources"]))
        n += 1
    return n


def company_discovery_scan(ctx) -> dict:
    """Weekly job: fans out across Tier 1 (all ATS-known companies) +
    Tier 2 (Himalayas/TheirStack, free-only) + GitHub, scores confidence,
    and updates company_intel. Honest skip with no target_role on file —
    nothing to search keyword/city queries against."""
    profile = ctx.store.get_career_profile(ctx.user_id) or {}
    target_role = (profile.get("target_role") or "").strip()
    if not target_role:
        return {"skipped": "no target_role on file"}

    cities = _discovery_cities(profile)
    tier1_hits = _tier1_leg(ctx)
    tier2_hits, tier2_availability = _tier2_leg(
        ctx, target_role, profile.get("target_location") or "")
    github_hits = _github_leg(ctx, cities, [target_role])

    merged = _merge_hits(tier1_hits, tier2_hits, github_hits)
    persisted = _persist_hits(ctx, merged)

    return {"companies_found": persisted, "tier2_availability": tier2_availability}


# ---------------------------------------------------------------------------
# LinkedIn slug — free search-lookup only, never a guess
# ---------------------------------------------------------------------------

def enrich_linkedin_slug(ctx, company: str) -> dict:
    from .connectors.mcp_call import ConnectorCallError, call_mcp_tool, extract_list

    if not company:
        return {"linkedin_slug": None}
    try:
        result = call_mcp_tool(ctx.user_id, ctx.store, "web_search",
                               ("web_search", "search", "brave_web_search", "tavily_search"),
                               {"query": f"{company} LinkedIn"}, target_style="none")
    except ConnectorCallError:
        return {"linkedin_slug": None}   # no web-search connector registered — honest

    for item in extract_list(result)[:5]:
        m = _LINKEDIN_COMPANY_RE.search(str(item.get("url") or ""))
        if m:
            slug = m.group(1)
            ctx.store._upsert_linkedin_slug(ctx.user_id, company, slug, "search_lookup")
            return {"linkedin_slug": slug, "linkedin_source": "search_lookup"}
    return {"linkedin_slug": None}


# ---------------------------------------------------------------------------
# Read/toggle surface
# ---------------------------------------------------------------------------

def list_companies(ctx, city: str | None = None, confidence: str | None = None,
                   is_target: bool | None = None) -> list[dict]:
    return ctx.store.list_company_intel(ctx.user_id, city=city, confidence=confidence,
                                        is_target=is_target)


def set_company_target(ctx, company_id: str, is_target: bool) -> bool:
    return ctx.store.set_company_is_target(ctx.user_id, company_id, is_target)


def company_postings(ctx, company_id: str) -> list[dict]:
    row = ctx.store.get_company_intel_by_id(ctx.user_id, company_id)
    if row is None:
        return []
    return [p for p in ctx.store.list_postings(ctx.user_id, limit=200)
           if p.get("company") == row["company"]]


def recent_fast_track_postings(ctx, limit: int = 20) -> list[dict]:
    postings = ctx.store.list_postings(ctx.user_id, limit=200)
    fast = [p for p in postings if (p.get("source") or "").startswith("ats_")]
    fast.sort(key=lambda p: p.get("discovered_at") or "", reverse=True)
    return fast[:limit]
