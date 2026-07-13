"""Career Intelligence Graph (CAREER AUTOPILOT Phase B) — a typed
relationship graph connecting skills, companies, projects, and target
roles, built on the SAME `GraphStore`/`graph.db` `amy/automation/
orchestrator.py`'s career-plan nodes (`agentgoal:`/`agenttask:`) already
live in — not a second graph system, and not a dedicated file the way
`amy/finance/aml_engine.py`'s circular-transfer graph is.

That AML graph used a DEDICATED file specifically because financial
account/beneficiary nodes in the shared graph would leak into
`career_apply.py`'s referral search (an untyped substring scan over
every node label) and the general knowledge-graph viz — a real cross-
domain sensitivity concern. No such concern exists here: `graph.db` is
already career-adjacent, and adding `company`/`skill`/`project`/
`target_role` nodes to it directly IMPROVES that same referral search
(more real nodes to match a company name against) instead of risking
anything. Node ids are namespaced (`skill:`/`company:`/`project:`/
`role:` prefixes) to avoid colliding with the existing `agentgoal:`/
`agenttask:` ids, matching that file's own convention.

Corollary: this module NEVER calls `GraphStore.reset()` — that would
wipe orchestrator.py's plan-graph nodes too. `add_node`/`add_edge` are
idempotent (INSERT OR REPLACE / ON CONFLICT UPDATE), so re-running
`rebuild_career_graph()` refreshes only this phase's own nodes/edges.
Known, disclosed limitation: edges are only ever ADDED, never pruned — a
skill a company stops mentioning in newer postings stays linked until a
future phase adds explicit staleness handling.

None of the three query functions below (`top_skill_gap`,
`companies_matching_profile`, `why_rejected`) actually depend on the
graph being freshly built — each is a direct, testable query over
`job_postings`/`applications`/`career_profile`, same style Phase A's
`skill_demand_report()` already uses. The graph serves exploration/
traversal (the existing generic `/api/kg/*` endpoints) and the referral
search, not as a read dependency for these three.

No salary/compensation numbers appear anywhere in this module's output —
there is no outcome/salary data anywhere in this system to support them.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Graph population
# ---------------------------------------------------------------------------

def _graph_path(ctx) -> str:
    return str(Path(ctx.finance_path).parent / "graph.db")


def rebuild_career_graph(ctx) -> dict:
    """Per active track (career_scout._active_tracks()): a target_role
    node; company+skill nodes and 'requires' edges from matched
    job_postings.keywords; project+skill nodes and 'demonstrates' edges
    from a live GitHub portfolio classify pass (best-effort — a missing
    GitHub connector just skips project nodes, never blocks the rest);
    'matched_by' edges from STORED job_postings.match_score (never
    recomputed); 'applied_to' edges from applications. Honest {"skipped":
    ...} if there's no target_role on file at all."""
    from .career_scout import _active_tracks, _track_all_words, _track_matches_posting
    from .knowledge_graph.store import GraphStore

    profile = ctx.store.get_career_profile(ctx.user_id) or {}
    tracks = _active_tracks(profile)
    if not tracks:
        return {"skipped": "no target_role on file"}

    all_postings = ctx.store.list_postings(ctx.user_id, limit=1000)
    applications = ctx.store.list_applications(ctx.user_id)
    posting_by_id = {p["id"]: p for p in all_postings}

    # One live GitHub portfolio classify pass, shared across all tracks —
    # same "cheap, no side effect" reuse career_apply.py's
    # _showcase_repo_names() already does for a single posting.
    project_edges: list[tuple[str, str, str]] = []   # (project_id, project_label, skill_kw)
    try:
        from . import tools
        from .agents.reactive import _classify_repos

        repo_out = tools.invoke(ctx, "portfolio_repo_list", {}, actor="agent")
        repos = repo_out.get("repos") or []
        all_keywords = {kw.strip() for p in all_postings for kw in (p.get("keywords") or [])
                        if kw.strip()}
        if repos and all_keywords:
            showcase, needs_work, _not_relevant = _classify_repos(repos, all_keywords)
            for r in showcase + needs_work:
                name = str(r.get("name") or r.get("full_name") or "").strip()
                if not name:
                    continue
                for kw in (r.get("_matched_keywords") or []):
                    project_edges.append((f"project:{name.lower()}", name, kw))
    except Exception:
        pass   # GitHub connector missing/unreachable — project nodes are best-effort

    g = GraphStore(_graph_path(ctx))
    stats_before = g.stats()
    try:
        for track in tracks:
            role_id = f"role:{track.lower()}"
            g.add_node(role_id, "target_role", track)

            matched = [p for p in all_postings if _track_matches_posting(track, p)]
            track_words = _track_all_words(track)

            for p in matched:
                company = (p.get("company") or "").strip()
                if not company:
                    continue
                company_id = f"company:{company.lower()}"
                g.add_node(company_id, "company", company)
                for kw in (p.get("keywords") or []):
                    kw = kw.strip()
                    if not kw or kw.lower() in track_words:
                        continue   # the track's own name isn't a skill
                    skill_id = f"skill:{kw.lower()}"
                    g.add_node(skill_id, "skill", kw)
                    g.add_edge(company_id, skill_id, "requires")

            for project_id, label, kw in project_edges:
                skill_id = f"skill:{kw.lower()}"
                g.add_node(project_id, "project", label)
                g.add_node(skill_id, "skill", kw)
                g.add_edge(project_id, skill_id, "demonstrates")

            scores_by_company: dict[str, list[float]] = defaultdict(list)
            for p in matched:
                company = (p.get("company") or "").strip()
                score = p.get("match_score")
                if company and score is not None:
                    scores_by_company[company].append(float(score))
            for company, scores in scores_by_company.items():
                avg = sum(scores) / len(scores)
                g.add_edge(role_id, f"company:{company.lower()}", "matched_by", weight=avg)

            applied_counts: dict[str, int] = defaultdict(int)
            for a in applications:
                posting = posting_by_id.get(a.get("posting_id"))
                if not posting or not _track_matches_posting(track, posting):
                    continue
                company = (posting.get("company") or "").strip()
                if company:
                    applied_counts[company] += 1
            for company, n in applied_counts.items():
                g.add_edge(role_id, f"company:{company.lower()}", "applied_to", weight=float(n))

        g.commit()
        stats_after = g.stats()
    finally:
        g.close()

    return {"tracks": tracks, "stats_before": stats_before, "stats_after": stats_after}


# ---------------------------------------------------------------------------
# Skill gap roadmap — reuses Phase A's skill_demand_report(), never
# recomputes keyword frequency itself.
# ---------------------------------------------------------------------------

def top_skill_gap(ctx, target_role: str) -> dict:
    from .career_scout import skill_demand_report

    report = skill_demand_report(ctx, target_role, propose=False)
    missing = [e for e in report["top_missing_skills"] if not e["in_profile"]]
    missing.sort(key=lambda e: e["frequency_pct"], reverse=True)
    return {
        "target_role": target_role,
        "postings_analyzed": report["postings_analyzed"],
        "missing_skills": [
            {"skill": e["skill"], "demand_pct": e["frequency_pct"], "order": i + 1}
            for i, e in enumerate(missing)
        ],
        "ordering_basis": "demand frequency across matched postings, most-demanded first",
    }


# ---------------------------------------------------------------------------
# Company match query — reuses STORED job_postings.match_score
# (career_scout.py's existing match-scoring logic), never recomputes.
# ---------------------------------------------------------------------------

def companies_matching_profile(ctx, min_avg_score: float = 70.0) -> dict:
    """Only aggregates postings that were actually scored (match_score IS
    NOT NULL) — an unscored posting is excluded from its company's
    average, never treated as a 0, mirroring career_scout.py's
    _score_postings() docstring convention ('missing index = not scored,
    not scored zero')."""
    postings = ctx.store.list_postings(ctx.user_id, limit=1000)
    scores_by_company: dict[str, list[float]] = defaultdict(list)
    for p in postings:
        company = (p.get("company") or "").strip()
        score = p.get("match_score")
        if company and score is not None:
            scores_by_company[company].append(float(score))

    companies = []
    for company, scores in scores_by_company.items():
        avg = sum(scores) / len(scores)
        if avg >= min_avg_score:
            companies.append({"company": company, "avg_match_score": round(avg, 1),
                             "scored_postings": len(scores)})
    companies.sort(key=lambda c: c["avg_match_score"], reverse=True)
    return {"min_avg_score": min_avg_score, "companies": companies}


# ---------------------------------------------------------------------------
# Rejection analysis — never a confident cause, only a graded correlation.
# ---------------------------------------------------------------------------

def why_rejected(ctx, application_id: str) -> dict:
    applications = ctx.store.list_applications(ctx.user_id)
    application = next((a for a in applications if a["id"] == application_id), None)
    if application is None:
        return {"available": False, "reason": "no such application"}
    if application.get("status") != "rejected":
        return {"available": False,
               "reason": f"application status is {application.get('status')!r}, not rejected"}

    posting = ctx.store.get_posting(ctx.user_id, application["posting_id"])
    keywords = [k.strip() for k in (posting.get("keywords") if posting else []) or []
               if k.strip()]

    # exclude role-shaped words from the user's own active track(s), same
    # exclusion skill_demand_report()/rebuild_career_graph() already apply —
    # a posting titled "Flutter Developer at Acme" isn't "missing" the
    # skill "Developer" just because that word isn't on the skill list.
    from .career_scout import _active_tracks, _track_all_words

    profile = ctx.store.get_career_profile(ctx.user_id) or {}
    track_noise: set[str] = set()
    for track in _active_tracks(profile):
        track_noise |= _track_all_words(track)
    keywords = [k for k in keywords if k.lower() not in track_noise]

    if not keywords:
        return {"available": True, "confidence": "none",
               "explanation": "This posting has no extracted keywords on file (beyond your "
                              "own target role's name), so a skill-gap explanation can't be "
                              "derived from available data. The rejection could reflect "
                              "competition, timing, or other factors this system has no "
                              "visibility into.",
               "missing_skills": []}

    have = {s.strip().lower() for s in (profile.get("skills") or [])}
    missing = [k for k in keywords if k.lower() not in have]

    if not missing:
        return {"available": True, "confidence": "none",
               "explanation": "Your CURRENT skill profile covers every keyword this posting "
                              "matched — skill gap doesn't look like a likely explanation. "
                              "The rejection could reflect competition, timing, or other "
                              "factors this system has no visibility into.",
               "missing_skills": []}

    confidence = "moderate" if len(missing) >= 3 else "low"
    return {"available": True, "confidence": confidence,
           "explanation": (f"{len(missing)} keyword(s) this posting mentioned aren't on "
                           f"your CURRENT skill profile: {', '.join(missing)}. This MAY be "
                           "a factor, but rejections can also reflect competition, timing, "
                           "or other things this system can't observe — not certain, just a "
                           "correlation worth noting."),
           "missing_skills": missing}
