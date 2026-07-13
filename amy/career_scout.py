"""JobScoutSensor + match scoring (CAREER AUTOPILOT Part 4).

Same Sensor pattern as amy/connectors/sensors.py's GitHubSensor/PlaneSensor:
wraps an existing read path (the job_search registry tool, which itself
wraps the jobspy MCP connector) and emits canonical events through the
injected EventStore. Lives as its own flat module (like amy/patterns.py,
amy/financing.py, ...) rather than under amy/connectors/ — job scouting is
career-domain logic on top of a generic MCP read tool, not a generic
connector capability the way GitHub/Plane sensors are.

Match scoring is a SINGLE batched LLM call per poll cycle (ranker.py's
pattern: one call scores every newly-discovered posting, not one call per
posting), sensitive=True because it reasons about the candidate's own
skills/profile (CLAUDE.md's sensitive-routing rule, same class as
GSTIN/PAN). Postings degrade to unscored (match_score stays NULL) rather
than blocking on any LLM failure — still saved, just re-eligible for
scoring never (today's design: score once at discovery time; a future
poll won't rescore an already-seen posting, since add_posting_if_new
dedups on url).

"Portfolio evidence" now uses REAL persisted data when available
(CAREER AUTOPILOT Phase D, amy/career_portfolio.py) — showcase repo
names + their matched keywords are injected into the match-scoring
prompt below, replacing the earlier "known simplification" (career_
profile.skills alone, portfolio_analyze's classification wasn't
persisted anywhere queryable). Still degrades to skills-only when no
portfolio_items exist yet (no GitHub connector, or portfolio never
analyzed) — never fabricates repo evidence that isn't on file.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from collections import Counter

from .operational.sensors import Sensor

_log = logging.getLogger("amy.career_scout")

# ---------------------------------------------------------------------------
# CAREER AUTOPILOT Phase A — per-posting keyword extraction. Deterministic,
# no LLM dependency (unlike match scoring above, which degrades to
# unscored on any LLM failure) so every discovered posting always gets
# keywords. Same tokenize/stopword-filter approach as amy/automation/
# orchestrator.py's _extract_keywords() (a proven pattern in this codebase
# for deriving representative terms from job posting text) — restated
# locally rather than importing that module's private helper/constant,
# same "small enough to restate" precedent set across the Banking Risk
# Intelligence phases (e.g. aml_engine.py's cash-spike signal vs
# fraud_engine.py's spend-spike). orchestrator.py's version aggregates
# frequency ACROSS multiple postings for a one-off gap analysis; this one
# extracts terms from ONE posting's own text, for the keywords column.
# ---------------------------------------------------------------------------

_SKILL_STOPWORDS = {"with", "that", "this", "from", "have", "will", "your",
                    "team", "work", "role", "years", "experience", "skills",
                    "strong", "ability", "using", "including", "across",
                    "other", "such", "also", "into", "about", "need",
                    "looking", "join", "help", "build", "develop", "working",
                    "must", "should", "which", "they", "them", "their",
                    "what", "when", "where", "company", "candidate",
                    # extended locally beyond orchestrator.py's original list —
                    # short function words that slip through the >=3-char regex
                    # and matter more here since a single posting is short text,
                    # unlike orchestrator.py's cross-posting frequency aggregation
                    # where they're diluted. Left orchestrator.py's own copy
                    # untouched (a "restate locally" precedent, not an import).
                    "and", "the", "for", "are", "was", "were", "can", "you",
                    "our", "new", "all", "any", "has", "not", "per", "job",
                    "one", "who", "may",
                    # common posting-boilerplate words — frequent in nearly
                    # every posting regardless of track, so they'd otherwise
                    # dominate the "top missing skill" ranking as noise
                    "requires", "required", "require", "requirements",
                    "preferred", "seeking", "responsibilities",
                    "qualifications", "description", "apply", "opportunity"}


def _extract_posting_keywords(title: str, description: str, max_keywords: int = 20) -> list[str]:
    """Distinct notable terms from a single posting's title+description —
    order-preserving, original casing of first occurrence kept."""
    text = f"{title} {description}"
    seen: dict[str, str] = {}
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9+.#]{2,}", text):
        w = raw.rstrip(".")   # strip a sentence-terminal period (Node.js/C++/C# keep theirs — they don't end with one)
        lw = w.lower()
        if lw in _SKILL_STOPWORDS or len(w) < 3:
            continue
        seen.setdefault(lw, w)
    return list(seen.values())[:max_keywords]

_MATCH_SYSTEM = (
    "Score how well each job posting matches the candidate's profile, "
    "0-100. Consider: skill overlap, experience fit, portfolio evidence "
    "(skills the candidate has that the posting wants), and location/"
    "remote fit. Be conservative — an ESTIMATE, not a guarantee. Respond "
    "with EXACTLY ONE JSON object: {\"scores\": [{\"index\": <n>, "
    "\"score\": <0-100>, \"factors\": {\"skill_overlap\": \"...\", "
    "\"experience_fit\": \"...\", \"portfolio_evidence\": \"...\", "
    "\"location_fit\": \"...\"}}]}"
)


def _match_threshold() -> float:
    from . import config
    try:
        return float(config._env("AMY_CAREER_MATCH_THRESHOLD", "70"))
    except ValueError:
        return 70.0


# jobspy quirk (see mcp_servers/jobspy_server.py): country_indeed MUST match
# the location's country or indeed silently returns ZERO results (found live:
# profile location "Bangalore" + the tool's USA default = empty scout runs).
# Derived from the user's home jurisdiction pack id, never guessed by an LLM.
_JURISDICTION_COUNTRY = {"india": "India", "us": "USA",
                         "uae": "United Arab Emirates"}


def _country_for_ctx(ctx) -> str | None:
    home = ((ctx._extras.get("jurisdictions") or ["india"])[0] or "").lower()
    return _JURISDICTION_COUNTRY.get(home)


def _scout_sites() -> str:
    """Boards to scout, comma-separated (AMY_JOB_SCOUT_SITES). Default covers
    the globally useful boards (Google Jobs aggregates regional boards like
    Naukri, the closest legitimate route to inventory that blocks direct
    scraping) plus Naukri itself — dominant in India, currently 406-blocked
    upstream but free to carry since the jobspy server scrapes each site
    independently: a blocked board only shrinks the result, never fails the
    search. Regional extras (bayt, bdjobs, zip_recruiter) are opt-in via
    the env var."""
    from . import config
    return config._env("AMY_JOB_SCOUT_SITES",
                       "indeed,linkedin,naukri,glassdoor,google").strip()


def _portfolio_evidence_line(ctx, max_repos: int = 3) -> str:
    """CAREER AUTOPILOT Phase D: real showcase-repo evidence for the match-
    scoring prompt, replacing the earlier skills-only simplification (see
    module docstring). Empty string (never fabricated) when no portfolio
    items are persisted yet — the LLM prompt just has one fewer line."""
    try:
        items = ctx.store.list_portfolio_items(ctx.user_id, classification="showcase")
    except Exception:
        return ""
    if not items:
        return ""
    parts = []
    for item in items[:max_repos]:
        kws = ", ".join(item.get("matched_keywords") or []) or "general relevance"
        parts.append(f"{item['repo_name']} ({kws})")
    return "Portfolio evidence (real showcase repos): " + "; ".join(parts)


def _score_postings(ctx, postings: list[dict], profile: dict) -> dict[int, dict]:
    """ONE batched LLM call scoring every posting in this cycle. Returns
    {index: {"score": float, "factors": dict}}; {} on any LLM/parse
    failure or when no LLM is available — callers must treat a missing
    index as 'not scored', not 'scored zero'."""
    from .agents.reactive import _get_llm

    if not postings:
        return {}
    llm = _get_llm(ctx)
    if llm is None:
        return {}

    lines = [f'{i}. {p.get("title", "")} at {p.get("company", "")} '
            f'({p.get("location", "")}) — {(p.get("description") or "")[:300]}'
            for i, p in enumerate(postings)]
    portfolio_line = _portfolio_evidence_line(ctx)
    prompt = (f"Candidate target role: {profile.get('target_role', '')}\n"
             f"Candidate skills: {', '.join(profile.get('skills') or []) or 'none on file'}\n"
             f"Candidate location: {profile.get('target_location', '')}"
             f"{' (remote OK)' if profile.get('remote_ok') else ''}\n"
             f"{portfolio_line}\n\n"
             "Postings:\n" + "\n".join(lines))
    try:
        text, provider = llm.generate(_MATCH_SYSTEM, prompt, sensitive=True)
        if provider == "template":
            return {}
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return {}
        parsed = json.loads(m.group(0))
        out: dict[int, dict] = {}
        for entry in parsed.get("scores") or []:
            try:
                idx = int(entry["index"])
                score = max(0.0, min(100.0, float(entry.get("score", 0))))
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= idx < len(postings):
                out[idx] = {"score": score, "factors": entry.get("factors") or {}}
        return out
    except Exception as exc:
        _log.warning("job_scout: match scoring failed, postings stay unscored: %s", exc)
        return {}


class JobScoutSensor(Sensor):
    name = "job_scout"

    def __init__(self, event_store, ctx):
        super().__init__(event_store)
        self.ctx = ctx

    def poll(self) -> list[dict]:
        """One poll cycle: no active career goal -> no-op. Otherwise query
        job_search for the goal's role/location, dedup against
        job_postings (add_posting_if_new), score the newly-discovered ones
        in one batched call, emit career.job_discovered per new posting,
        and notify for anything at/above the match threshold. Any
        connector/LLM failure degrades to a shorter result, never raises
        (the caller is a periodic job tick)."""
        from . import tools
        from .events.store import CAREER_JOB_DISCOVERED

        emitted: list[dict] = []
        goal = self.ctx.collab.conn.execute(
            "SELECT id, career_meta FROM goals WHERE domain='career' AND status='active'"
            " ORDER BY created_at DESC LIMIT 1").fetchone()
        if goal is None:
            return emitted

        target_role = None
        try:
            target_role = (json.loads(goal["career_meta"] or "{}") or {}).get("target_role")
        except Exception:
            pass
        profile = self.ctx.store.get_career_profile(self.ctx.user_id) or {}
        target_role = target_role or profile.get("target_role")
        if not target_role:
            return emitted

        search_args = {"search_term": target_role,
                       "location": profile.get("target_location") or "",
                       "is_remote": bool(profile.get("remote_ok")),
                       "results_wanted": 20,
                       "site_names": _scout_sites()}
        country = _country_for_ctx(self.ctx)
        if country:
            search_args["country_indeed"] = country
        try:
            out = tools.invoke(self.ctx, "job_search", search_args, actor="agent")
        except Exception as exc:
            _log.warning("job_scout: job_search failed: %s", exc)
            return emitted

        new_postings: list[dict] = []
        new_ids: list[str] = []
        for job in (out.get("jobs") or []):
            url = str(job.get("job_url") or job.get("url") or "").strip()
            if not url:
                continue
            title = job.get("title") or ""
            description = job.get("description") or ""
            posting = {"title": title, "company": job.get("company") or "",
                      "url": url, "location": job.get("location") or "",
                      "salary": job.get("salary") or job.get("min_amount") or "",
                      "is_remote": bool(job.get("is_remote")),
                      "description": description,
                      "source": job.get("site") or "jobspy",   # which board found it
                      "keywords": _extract_posting_keywords(title, description)}
            pid, is_new = self.ctx.store.add_posting_if_new(self.ctx.user_id, posting)
            if is_new:
                new_postings.append(posting)
                new_ids.append(pid)

        if not new_postings:
            return emitted

        scores = _score_postings(self.ctx, new_postings, profile)
        threshold = _match_threshold()
        ns = self.ctx.notify_store()
        for i, posting in enumerate(new_postings):
            pid = new_ids[i]
            score_entry = scores.get(i)
            if score_entry:
                self.ctx.store.set_posting_match(
                    self.ctx.user_id, pid, score_entry["score"], score_entry["factors"])
            payload = {"posting_id": pid, "title": posting["title"],
                      "company": posting["company"], "url": posting["url"],
                      "goal_id": goal["id"],
                      "match_score": score_entry["score"] if score_entry else None}
            self.publish(CAREER_JOB_DISCOVERED, payload)
            emitted.append(payload)

            if score_entry and score_entry["score"] >= threshold:
                ref = f"career_match_{pid}"
                if not ns.exists_today("career_job_match", ref):
                    ns.create(
                        type="career_job_match",
                        title=f"Strong match ({score_entry['score']:.0f}/100): "
                              f"{posting['title']} at {posting['company']}",
                        body=(f"Estimated match {score_entry['score']:.0f}/100 for "
                             f"'{target_role}'. Factors: " +
                             "; ".join(f"{k}: {v}" for k, v in
                                      (score_entry.get('factors') or {}).items())),
                        priority="normal",
                        related_entity={"id": ref, "entity_type": "job_posting",
                                        "posting_id": pid})
                self._maybe_propose_application(pid, goal["id"])
        return emitted

    def _maybe_propose_application(self, posting_id: str, goal_id: str) -> None:
        """CAREER AUTOPILOT Part 5: 'the agent proposes for high scores' —
        gated by AMY_AGENT_APPLICATION_TRACKER (separate from AMY_AGENT_
        JOB_SCOUT, which only gates discovery/scoring). prepare_application
        itself always routes the actual send through tools.invoke(actor=
        "agent"), so this still lands as one approval, never an auto-send —
        the dedup key (apply_{posting_id}) makes a repeat call harmless."""
        from . import config
        if not config.agent_enabled("application_tracker"):
            return
        try:
            from .career_apply import prepare_application
            prepare_application(self.ctx, posting_id, goal_id=goal_id)
        except Exception as exc:
            _log.warning("job_scout: auto-apply proposal failed for %s: %s",
                        posting_id, exc)


def job_scout_poll(ctx) -> dict:
    """Job handler entry point (job_scout_poll job, default every 12h).
    Re-checks the kill switch here too, not just at job registration: job
    rows persist in automation_jobs after the env flag is turned off (same
    stance as learning_feed_refresh)."""
    from . import config
    if not config.agent_enabled("job_scout"):
        return {"skipped": "AMY_AGENT_JOB_SCOUT is off"}
    emitted = JobScoutSensor(ctx.events(), ctx).poll()
    return {"discovered": len(emitted)}


# ---------------------------------------------------------------------------
# CAREER AUTOPILOT Phase A — "Learning Driven by Jobs": aggregate
# job_postings.keywords into a per-track market-demand report, and propose
# Learning Feed focuses for frequently-demanded skills not yet on the
# profile. Pure aggregation over data JobScoutSensor already collected —
# no external data source, no fabrication risk.
# ---------------------------------------------------------------------------

SKILL_DEMAND_WINDOW_DAYS = 90        # tunable default, not sourced from any market analysis
SKILL_DEMAND_MAX_POSTINGS = 100      # tunable default, not sourced from any market analysis
SKILL_DEMAND_PROPOSAL_THRESHOLD_PCT = 25   # tunable default, not sourced from any market analysis
SKILL_DEMAND_MAX_PROPOSALS_PER_RUN = 3     # keeps one report run from flooding the Approval Inbox

_TRACK_SPLIT_RE = re.compile(r"\s*(?:,|/|&|\band\b)\s*", re.IGNORECASE)
_TRACK_STOPWORDS = {"engineer", "developer", "role", "position", "track"}


def _active_tracks(profile: dict) -> list[str]:
    """career_profile.target_role is a single TEXT column, but a user can
    (and, per this feature's own motivating example, does) track several
    roles in it at once, e.g. 'Flutter Developer / Mobile Engineer / AI
    Mobile Engineer / GenAI Engineer'. Splits on common delimiters into a
    deduped list — the common single-role case still returns a
    single-item list untouched."""
    raw = (profile.get("target_role") or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in _TRACK_SPLIT_RE.split(raw) if p.strip()]
    seen: dict[str, str] = {}
    for p in parts:
        seen.setdefault(p.lower(), p)
    return list(seen.values())


def _track_all_words(track: str) -> set[str]:
    """EVERY word (>=3 chars) in a track name, lowercased, including
    generic role suffixes like 'developer'/'engineer'. Used by
    skill_demand_report() to exclude the track's own name from the
    counted keywords — every posting for 'Flutter Developer' mentions
    both 'Flutter' AND 'Developer' in its title by construction, neither
    of which is a skill gap, just how the postings were found."""
    return {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9+]{2,}", track)}


def _track_words(track: str) -> list[str]:
    """Significant words (>=4 chars, not a generic role-shaped stopword)
    from a track name, lowercased. Used by _track_matches_posting() for
    classifying whether a posting belongs to this track — deliberately
    EXCLUDES generic role suffixes ('developer'/'engineer'), since
    matching on those alone would match nearly any tech posting. NOT used
    for the keyword-exclusion purpose above — see _track_all_words()."""
    return [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9+]{3,}", track)
           if w.lower() not in _TRACK_STOPWORDS]


def _track_matches_posting(track: str, posting: dict) -> bool:
    """Heuristic classifier — job_postings has no stored 'which track was
    this searched under' column (only ONE active career goal's target_role
    is ever searched per poll cycle today), so this is applied uniformly
    to every posting, old and new, rather than trusting a partial stored
    tag. A posting matches a track if any significant word from the track
    name appears in the posting's title or description."""
    text = f"{posting.get('title', '')} {posting.get('description', '')}".lower()
    words = _track_words(track)
    if not words:
        return track.lower() in text
    return any(re.search(r"\b" + re.escape(w) + r"\b", text) for w in words)


def _propose_focuses_for_demand(ctx, track: str, missing_skills: list[dict]) -> list[dict]:
    """missing_skills: top_missing_skills already sorted by frequency_pct
    desc. Proposes create_learning_focus (RISK_WRITE, routed through
    AGENT_GATE — same tier-config-driven pattern _learning_agent uses for
    goal proposals in amy/agents/reactive.py, no new tier rule invented
    here) for up to SKILL_DEMAND_MAX_PROPOSALS_PER_RUN qualifying skills.
    Checks list_focuses() for an existing same-topic row first — add_focus
    itself has no dedup at the DB layer, and the dedup_key below only
    protects against re-proposing while a prior approval is still
    pending/executed, not against a focus that already exists some other
    way (manually added, a different agent, ...). Deliberately a direct
    query, NOT learning_feed.sensor.list_focuses() — that function
    auto-seeds a default focus for a user with zero rows (a real, if
    surprising, side effect meant for the Learn tab's first-time UX, not
    for a dedup check that must stay read-only)."""
    from . import tools

    existing_topics = {r["topic"].strip().lower() for r in ctx.collab.conn.execute(
        "SELECT topic FROM learning_focuses WHERE uid=? AND active=1",
        (ctx.user_id,)).fetchall()}
    proposed: list[dict] = []
    for entry in missing_skills:
        if len(proposed) >= SKILL_DEMAND_MAX_PROPOSALS_PER_RUN:
            break
        if entry["frequency_pct"] < SKILL_DEMAND_PROPOSAL_THRESHOLD_PCT:
            continue
        skill = entry["skill"]
        if skill.strip().lower() in existing_topics:
            proposed.append({"skill": skill, "status": "already_tracked"})
            continue
        reasoning = (f"'{skill}' appears in {entry['frequency_pct']:.0f}% of recently "
                    f"discovered '{track}' postings and isn't on your profile's skill "
                    "list — proposing a Learning Feed focus to track it.")
        ctx._extras["agent_name"] = "career_skill_demand"
        ctx._extras["agent_reasoning"] = reasoning
        ctx._extras["agent_dedup_key"] = f"learning_focus_skill_{track}_{skill}".lower()
        try:
            result = tools.invoke(ctx, "create_learning_focus", {"topic": skill}, actor="agent")
            proposed.append({"skill": skill, "status": "proposed", "result": result})
        except Exception as exc:
            _log.warning("skill_demand: proposing focus for %r failed: %s", skill, exc)
            proposed.append({"skill": skill, "status": "failed", "error": str(exc)})
    return proposed


def skill_demand_report(ctx, track: str, propose: bool = True) -> dict:
    """Pure aggregation over job_postings.keywords for ONE track, within
    SKILL_DEMAND_WINDOW_DAYS/SKILL_DEMAND_MAX_POSTINGS. When propose=True
    (the tool/route default), also proposes learning focuses for
    qualifying missing skills — see _propose_focuses_for_demand()."""
    profile = ctx.store.get_career_profile(ctx.user_id) or {}
    have = {s.strip().lower() for s in (profile.get("skills") or [])}

    cutoff = (_dt.datetime.now(_dt.timezone.utc)
             - _dt.timedelta(days=SKILL_DEMAND_WINDOW_DAYS)).isoformat()
    all_postings = ctx.store.list_postings(ctx.user_id, limit=SKILL_DEMAND_MAX_POSTINGS * 5)
    recent = [p for p in all_postings if (p.get("discovered_at") or "") >= cutoff]
    matched = [p for p in recent if _track_matches_posting(track, p)]
    matched.sort(key=lambda p: p.get("discovered_at") or "", reverse=True)
    matched = matched[:SKILL_DEMAND_MAX_POSTINGS]

    track_words = _track_all_words(track)
    counts: Counter = Counter()
    for p in matched:
        for kw in dict.fromkeys(k.strip() for k in (p.get("keywords") or []) if k.strip()):
            if kw.lower() in track_words:
                continue   # the track's own name isn't a skill gap — every
                          # matched posting mentions it by construction
            counts[kw] += 1

    n = len(matched)
    top_missing = []
    for skill, count in counts.most_common():
        pct = round(100 * count / n, 1) if n else 0.0
        in_profile = skill.strip().lower() in have
        top_missing.append({"skill": skill, "frequency_pct": pct, "in_profile": in_profile})
    top_missing.sort(key=lambda e: e["frequency_pct"], reverse=True)

    report = {
        "computed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "postings_analyzed": n,
        "track": track,
        "top_missing_skills": top_missing[:20],
    }

    if propose:
        missing_only = [e for e in top_missing if not e["in_profile"]]
        report["proposed_focuses"] = _propose_focuses_for_demand(ctx, track, missing_only)
    else:
        report["proposed_focuses"] = []

    try:
        from .events.factory import get_events
        from .events.store import CAREER_SKILL_DEMAND_UPDATED
        get_events(ctx.user_id, ctx.collab, ctx=ctx).emit(
            CAREER_SKILL_DEMAND_UPDATED,
            {"track": track, "postings_analyzed": n,
            "top_skill": top_missing[0]["skill"] if top_missing else None},
            source="career_skill_demand")
    except Exception:
        pass

    return report


def skill_demand_reports(ctx, propose: bool = True) -> list[dict]:
    """One skill_demand_report() per active track (career_profile.
    target_role split — see _active_tracks()). Honestly [] when there's
    no career_profile or no target_role on file, never fabricated."""
    profile = ctx.store.get_career_profile(ctx.user_id) or {}
    tracks = _active_tracks(profile)
    return [skill_demand_report(ctx, track, propose=propose) for track in tracks]
