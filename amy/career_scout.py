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

Known simplification: the "portfolio evidence" scoring factor is inferred
from career_profile.skills only — portfolio_analyze's SHOWCASE/GAPS
classification (Part 3) isn't persisted anywhere queryable outside its
vault note, so there's no richer signal to pass here yet. Worth revisiting
if skills isn't proving representative enough in practice.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re

from .operational.sensors import Sensor

_log = logging.getLogger("amy.career_scout")

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
    prompt = (f"Candidate target role: {profile.get('target_role', '')}\n"
             f"Candidate skills: {', '.join(profile.get('skills') or []) or 'none on file'}\n"
             f"Candidate location: {profile.get('target_location', '')}"
             f"{' (remote OK)' if profile.get('remote_ok') else ''}\n\n"
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

        try:
            out = tools.invoke(
                self.ctx, "job_search",
                {"search_term": target_role,
                 "location": profile.get("target_location") or "",
                 "is_remote": bool(profile.get("remote_ok")),
                 "results_wanted": 20},
                actor="agent")
        except Exception as exc:
            _log.warning("job_scout: job_search failed: %s", exc)
            return emitted

        new_postings: list[dict] = []
        new_ids: list[str] = []
        for job in (out.get("jobs") or []):
            url = str(job.get("job_url") or job.get("url") or "").strip()
            if not url:
                continue
            posting = {"title": job.get("title") or "", "company": job.get("company") or "",
                      "url": url, "location": job.get("location") or "",
                      "salary": job.get("salary") or job.get("min_amount") or "",
                      "is_remote": bool(job.get("is_remote")),
                      "description": job.get("description") or "",
                      "keywords": []}
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
        return emitted


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
