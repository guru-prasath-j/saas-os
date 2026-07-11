"""Orchestrator agent (Phase R4) — natural-language goal → plan → gated tools.

Grown from the assistant's loop (same one-JSON-object protocol, same
provider-retry), with three upgrades:
  1. an explicit PLAN produced first and persisted,
  2. every tool call runs with actor="agent", so the R3 approval gate parks
     anything write/destructive — the orchestrator can *propose* freely but
     never act on data without the human,
  3. plan → steps → outcomes stored as GraphStore nodes/edges and the run
     journaled to the vault.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import uuid
from pathlib import Path

import re

from .assistant import _catalog
from .executors import JobCtx

_log = logging.getLogger("amy.automation.orchestrator")

_MAX_TOOL_CALLS = 10
_PLAN_MAX_STEPS = 4     # was 6 — with a slow thinking-model each extra step
                        # is 1-2 more long LLM calls; 4 keeps runs bounded
_TIME_BUDGET_S = 300    # wall clock: past this, remaining steps are skipped
                        # and the run summarizes what it did get done


def _first_obj(raw: str) -> dict | None:
    """First complete JSON object in the response — like the assistant's
    _parse_step but without its tool/final key filter (plans and summaries
    are arbitrary objects)."""
    raw = re.sub(r"```(?:json)?", "", raw or "").strip()
    decoder = json.JSONDecoder()
    idx = raw.find("{")
    while idx != -1:
        try:
            obj, _end = decoder.raw_decode(raw, idx)
        except Exception:
            idx = raw.find("{", idx + 1)
            continue
        if isinstance(obj, dict):
            return obj
        idx = raw.find("{", idx + 1)
    return None


# ---------------------------------------------------------------------------
# Storage (agent_goals table in collab.db)
# ---------------------------------------------------------------------------

def _ensure_table(ctx: JobCtx):
    ctx.collab.conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_goals ("
        " id TEXT PRIMARY KEY, ts TEXT, goal TEXT, plan TEXT,"
        " steps TEXT, summary TEXT, status TEXT)")
    ctx.collab.conn.commit()


def list_goal_runs(ctx: JobCtx, limit: int = 20) -> list[dict]:
    _ensure_table(ctx)
    rows = ctx.collab.conn.execute(
        "SELECT * FROM agent_goals ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["plan"] = json.loads(d["plan"] or "[]")
        d["steps"] = json.loads(d["steps"] or "[]")
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# LLM plumbing
# ---------------------------------------------------------------------------

def _gen(ctx: JobCtx, system: str, prompt: str, sensitive: bool = False) -> dict | None:
    for _ in range(2):   # one retry on provider flake
        try:
            # fast=True: each call here is a one-JSON-object decision, not
            # analysis — thinking mode made these 46s median (measured)
            raw, _p = ctx.llm.generate(system, prompt, sensitive=sensitive, fast=True)
            return _first_obj(raw)
        except Exception:
            continue
    return None


def _context_block(ctx: JobCtx) -> str:
    """Situational awareness via ContextModule over recent persisted events."""
    try:
        from ..context import ContextModule
        cm = ContextModule(ctx.events())
        for ev in reversed(ctx.events().recent(n=30)):
            cm._on_event(ev)
        return cm.get_context(15)
    except Exception:
        return "No recent activity."


# ---------------------------------------------------------------------------
# Graph persistence
# ---------------------------------------------------------------------------

def _store_plan_graph(ctx: JobCtx, run_id: str, goal: str,
                      plan: list[str]) -> list[str]:
    """goal node + one task node per step; belongs_to + depends_on edges.
    Returns task node ids (indexed by step)."""
    from ..knowledge_graph.store import GraphStore
    g = GraphStore(str(Path(ctx.finance_path).parent / "graph.db"))
    try:
        goal_node = f"agentgoal:{run_id}"
        g.add_node(goal_node, "goal", goal[:120], ref=f"agent_goals/{run_id}")
        task_ids = []
        for i, step in enumerate(plan):
            tid = f"agenttask:{run_id}:{i}"
            g.add_node(tid, "task", step[:120], ref="planned")
            g.add_edge(tid, goal_node, "belongs_to")
            if task_ids:
                g.add_edge(tid, task_ids[-1], "depends_on")
            task_ids.append(tid)
        g.commit()
        return task_ids
    finally:
        g.conn.close()


def _mark_task(ctx: JobCtx, task_id: str, label: str, outcome: str):
    from ..knowledge_graph.store import GraphStore
    g = GraphStore(str(Path(ctx.finance_path).parent / "graph.db"))
    try:
        g.add_node(task_id, "task", label[:120], ref=outcome[:400])
        g.commit()
    finally:
        g.conn.close()


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------

_PLAN_SYSTEM = (
    "You are Amy's orchestrator. Turn the user's goal into a short concrete "
    "plan using the available tools.\n\nTools:\n{catalog}\n\n"
    "Respond with EXACTLY ONE JSON object:\n"
    '  {{"plan": ["step 1", "step 2", ...], "reasoning": "why this plan"}}\n'
    f"Max {_PLAN_MAX_STEPS} steps. Steps must be achievable with the tools "
    "(reads for analysis, writes become approval requests for the user)."
)

_STEP_SYSTEM = (
    "You are Amy's orchestrator executing a plan step by step. "
    "Tools marked [write]/[destructive] are PARKED for the user's approval "
    "when you call them — that still counts as completing the step "
    "(proposing is your job; the human decides).\n\nTools:\n{catalog}\n\n"
    "Respond with EXACTLY ONE JSON object, one of:\n"
    '  {{"tool": "<name>", "args": {{...}}, "reasoning": "why this call"}}\n'
    '  {{"step_done": "<what this step concluded>"}}\n'
    "Never invent data — read it with tools first."
)

_SUMMARY_SYSTEM = (
    "Summarize this orchestrator run for the user in 2-4 sentences: what was "
    "analyzed, what was found, and what is now waiting for their approval. "
    'Respond with EXACTLY ONE JSON object: {"summary": "..."}'
)


def _persist_run(ctx: JobCtx, run_id: str, goal: str, plan: list,
                 steps: list, summary: str, status: str):
    ctx.collab.conn.execute(
        "INSERT OR REPLACE INTO agent_goals(id,ts,goal,plan,steps,summary,status)"
        " VALUES(?,?,?,?,?,?,?)",
        (run_id, _dt.datetime.now(_dt.timezone.utc).isoformat(), goal,
         json.dumps(plan), json.dumps(steps, default=str), summary, status))
    ctx.collab.conn.commit()



# ---------------------------------------------------------------------------
# Career plan template (CAREER AUTOPILOT Part 2)
#
# The generic loop above plans a max of _PLAN_MAX_STEPS (4) LLM-improvised
# tool calls in one pass — fine for "cut spending 10%", not enough to fan a
# career goal out across Learning Focus, Plane milestones, a portfolio
# first-look, and a persisted career profile (docs/AGENT_PLAN.md's CAREER
# AUTOPILOT pre-flight finding 7). Goals matching a career shape run this
# hardcoded fan-out instead — same "detect a known shape, run a template"
# pattern jurisdiction packs and the Learning Feed's focus->goal linkage
# already use elsewhere. Every WRITE still goes through
# tools.invoke(actor="agent"), so AGENT_GATE still gates it; only the goal/
# milestone bookkeeping the orchestrator does about ITS OWN plan (like
# _store_plan_graph above) happens directly — the same line the generic
# path already draws for its GraphStore/agent_goals writes.
# ---------------------------------------------------------------------------

_CAREER_ROLE_WORDS = ("engineer", "developer", "dev", "designer", "scientist",
                     "analyst", "manager", "architect", "specialist",
                     "consultant", "researcher", "programmer")
_CAREER_ACTION_PHRASES = ("become a", "become an", "land a job", "get a job",
                          "get hired", "break into", "transition into",
                          "transition to", "switch to", "switch career",
                          "new career", "career change", "career goal")

_CAREER_PARSE_SYSTEM = (
    "Extract the target job role and duration from this career goal. "
    "Respond with EXACTLY ONE JSON object: "
    '{"target_role": "<role to apply for NEXT>", '
    '"north_star_role": "<longer-term destination role, or null>", '
    '"weeks": <int>}. If the goal names a career LADDER ("become X then Y", '
    '"X en route to Y", "X, eventually Y"), target_role is the immediate '
    "role and north_star_role the destination; otherwise north_star_role is "
    "null. If no duration is stated, estimate a reasonable one (8-12 weeks)."
)

# Deterministic ladder split for the no-LLM fallback path: "X then Y",
# "X en route to Y", "X toward(s) Y", "X eventually Y".
_LADDER_SPLIT_RE = re.compile(
    r"\s+(?:then|en route to|towards?|eventually)\s+(?:a\s+|an\s+)?",
    re.IGNORECASE)

_SKILL_GAP_SYSTEM = (
    "Compare the candidate's current skills against what real job postings "
    "for this role ask for. List 3-6 concrete skill/technology gaps worth "
    "learning next, ordered by impact. Respond with EXACTLY ONE JSON "
    'object: {"gaps": ["<topic>", ...]}'
)

_SKILL_STOPWORDS = {"with", "that", "this", "from", "have", "will", "your",
                    "team", "work", "role", "years", "experience", "skills",
                    "strong", "ability", "using", "including", "across",
                    "other", "such", "also", "into", "about", "need",
                    "looking", "join", "help", "build", "develop", "working",
                    "must", "should", "which", "they", "them", "their",
                    "what", "when", "where", "company", "candidate"}


def _is_career_goal(text: str) -> bool:
    """Heuristic detector for a career-shaped goal ('become a GenAI engineer
    in 2 months'). Requires an action phrase ('become a', 'switch to', ...)
    AND a role-ish noun together (not either alone — 'become debt-free' or
    'become a morning person' shouldn't misfire), or an explicit 'career'
    mention."""
    low = text.lower()
    if "career" in low:
        return True
    has_action = any(a in low for a in _CAREER_ACTION_PHRASES)
    has_role = any(r in low for r in _CAREER_ROLE_WORDS)
    return has_action and has_role


def _log_career_template_failure(ctx: JobCtx, run_id: str, goal: str, exc: Exception) -> None:
    _log.warning("career plan template failed for run %s (%r): %s", run_id, goal, exc)
    try:
        ctx.events().emit("agent.error",
                          {"agent": "career_goal", "error": str(exc)[:400],
                           "reasoning": "career template raised; falling back "
                                        "to the generic planner"},
                          source="career_goal_agent")
    except Exception:
        pass


def _extract_role_and_deadline(ctx: JobCtx, goal: str) -> tuple[str, str, int, str | None]:
    """One LLM call to pull {target_role, north_star_role, weeks} out of the
    free-text goal — NOT sensitive, it's parsing the goal sentence itself,
    not resume/profile data. Degrades to a naive fallback (whole goal text
    as role, ladder split on 'then'/'en route to'/'toward', 8 weeks) on any
    LLM failure, same stance as every other degrade-gracefully call in this
    codebase (learning_feed/ranker.py, finance/budget_suggest.py, ...).

    Part 5F career ladder: target_role is the role to APPLY for next (drives
    scouting/ATS/drafts); north_star_role, when the goal names a longer-term
    destination, drives learning focuses / milestones / portfolio analysis."""
    resp = _gen(ctx, _CAREER_PARSE_SYSTEM, f"Goal: {goal}") if ctx.llm else None
    weeks = 8
    role = goal[:120]
    north_star: str | None = None
    if resp and resp.get("target_role"):
        role = str(resp["target_role"])[:120]
        ns = resp.get("north_star_role")
        if ns and str(ns).strip().lower() not in ("null", "none", ""):
            north_star = str(ns)[:120]
        try:
            weeks = max(1, int(resp.get("weeks") or 8))
        except (TypeError, ValueError):
            weeks = 8
    else:
        parts = _LADDER_SPLIT_RE.split(goal, maxsplit=1)
        if len(parts) == 2 and parts[1].strip():
            role, north_star = parts[0][:120].strip(), parts[1][:120].strip()
            # strip the leading action phrase ("become an ...") so the role
            # is comparable/searchable — longest phrase first, else
            # "become a" eats "become an"'s prefix and leaves "n ..."
            low = role.lower()
            for p in sorted(_CAREER_ACTION_PHRASES, key=len, reverse=True):
                if low.startswith(p):
                    role = role[len(p):].strip() or role
                    break
    if north_star and north_star.strip().lower() in role.strip().lower():
        north_star = None   # "X then X" is not a ladder
    deadline = (_dt.date.today() + _dt.timedelta(weeks=weeks)).isoformat()
    return role, deadline, weeks, north_star


def _extract_keywords(postings: list[dict], top_n: int = 8) -> list[str]:
    """Deterministic keyword-frequency fallback for _skill_gaps() when no
    LLM is available — never as good as the LLM pass, just never blocks the
    plan (mirrors ranker.py's 'return items unranked' stance)."""
    from collections import Counter
    counts: Counter = Counter()
    for p in postings:
        text = f"{p.get('title', '')} {p.get('description', '')}"
        for w in re.findall(r"[A-Za-z][A-Za-z0-9+.#]{2,}", text):
            if w.lower() in _SKILL_STOPWORDS or len(w) < 3:
                continue
            counts[w] += 1
    return [w for w, _n in counts.most_common(top_n)]


def _skill_gaps(ctx: JobCtx, target_role: str, profile: dict) -> list[str]:
    """Real postings (job_search tool) + career_profile.skills -> LLM skill-
    gap analysis. sensitive=True: this call reasons about the user's own
    skills, same class of data as GSTIN/PAN (CLAUDE.md quirk on sensitive
    routing). Degrades to a deterministic keyword-diff on any LLM/job_search
    failure — never blocks the plan."""
    from .. import tools
    postings: list[dict] = []
    try:
        out = tools.invoke(ctx, "job_search",
                           {"search_term": target_role, "results_wanted": 10},
                           actor="agent")
        postings = out.get("jobs") or []
    except Exception as exc:
        _log.warning("career template: job_search failed during skill-gap analysis: %s", exc)

    have = {s.lower() for s in (profile.get("skills") or [])}
    fallback = [k for k in _extract_keywords(postings) if k.lower() not in have][:5]

    if ctx.llm is None or not postings:
        return fallback or [target_role]

    desc_sample = "\n".join((p.get("description") or "")[:400] for p in postings[:6])
    prompt = (f"Target role: {target_role}\n"
             f"Current skills: {', '.join(profile.get('skills') or []) or 'none on file'}\n\n"
             f"Sample postings:\n{desc_sample}")
    resp = _gen(ctx, _SKILL_GAP_SYSTEM, prompt, sensitive=True)
    gaps = resp.get("gaps") if resp else None
    if isinstance(gaps, list) and gaps:
        return [str(g)[:80] for g in gaps[:6]]
    return fallback or [target_role]


def _weekly_milestones(target_role: str, weeks: int, gaps: list[str],
                       learn_role: str | None = None) -> list[str]:
    """Deterministic phase breakdown (skills 40% / portfolio 25% /
    applications 25% / interview prep 10%) — not LLM-dependent, so this
    step never fails regardless of provider availability.

    learn_role (Part 5F ladder): skill/portfolio phases build toward the
    north-star role; application/interview phases stay on the immediate
    target_role. Same role for both when there's no ladder.

    Granularity adapts to the horizon: a 52-week plan used to emit 52
    near-identical one-line rows ("Week 14: Skill building — X" x21 —
    user-reported as useless noise); phases now emit a handful of
    multi-week BLOCKS, each with a concrete outcome, so a year-long goal
    reads as ~a dozen real milestones instead of a wall of repetition."""
    weeks = max(weeks, 4)
    learn_role = learn_role or target_role
    skill_weeks = max(1, round(weeks * 0.4))
    portfolio_weeks = max(1, round(weeks * 0.25))
    apply_weeks = max(1, round(weeks * 0.25))
    interview_weeks = max(1, weeks - skill_weeks - portfolio_weeks - apply_weeks)

    gap_list = [g for g in (gaps or []) if str(g).strip()] or [learn_role]
    out: list[str] = []
    cursor = 1

    def _span(length: int) -> str:
        nonlocal cursor
        start, end = cursor, cursor + length - 1
        cursor = end + 1
        return f"Week {start}" if length == 1 else f"Weeks {start}-{end}"

    def _blocks(total_weeks: int, n_blocks: int) -> list[int]:
        n_blocks = max(1, min(n_blocks, total_weeks))
        base, rem = divmod(total_weeks, n_blocks)
        return [base + (1 if i < rem else 0) for i in range(n_blocks)]

    # skills: one block per gap (up to 6) — each block is a named topic
    # with a shippable outcome, not a repeated one-liner
    for i, length in enumerate(_blocks(skill_weeks, min(6, len(gap_list)))):
        gap = gap_list[i % len(gap_list)]
        out.append(f"{_span(length)}: Skill building — {gap}: course/docs, "
                   "then a small working demo committed to GitHub")
    # portfolio: up to 3 substantial projects themed on the top gaps
    for i, length in enumerate(_blocks(portfolio_weeks, min(3, portfolio_weeks))):
        theme = gap_list[i % len(gap_list)]
        out.append(f"{_span(length)}: Portfolio project #{i + 1} for "
                   f"{learn_role} — a {theme} project with README, tests "
                   "and a runnable demo")
    # applications: one block (two for long horizons) with a concrete cadence
    for length in _blocks(apply_weeks, 1 if apply_weeks <= 8 else 2):
        out.append(f"{_span(length)}: Applications — {target_role} roles: "
                   "steady weekly cadence, resume tailored per posting, "
                   "follow-ups tracked")
    # interview prep: one block
    out.append(f"{_span(interview_weeks)}: Interview prep for {target_role} — "
               "mock interviews, DSA/system-design reps, company research")
    return out


def _run_career_template(ctx: JobCtx, goal: str, run_id: str) -> dict:
    from .. import tools
    from ..autonomous import GoalEngine

    plan = [
        "Parse target role and timeline from the goal",
        "Create the career goal and profile",
        "Identify skill gaps from real job postings",
        "Create weekly milestones + a batched task breakdown",
        "Pull portfolio repos for a first look",
    ]
    task_ids = _store_plan_graph(ctx, run_id, goal, plan)
    steps_log: list[dict] = []
    queued = 0

    def _log_step(i: int, tool_name: str, args: dict, result, reasoning: str) -> None:
        nonlocal queued
        is_pending = isinstance(result, dict) and result.get("status") == "pending"
        ok = not (isinstance(result, dict) and result.get("error"))
        if is_pending:
            queued += 1
        steps_log.append({"step": i, "tool": tool_name, "args": args,
                          "reasoning": reasoning, "ok": ok,
                          "queued": is_pending, "result": result})
        if i < len(task_ids):
            _mark_task(ctx, task_ids[i], plan[i],
                      "queued for approval" if is_pending else str(result)[:200])

    # 1. parse target role + deadline (+ optional north star — Part 5F:
    # "become X then Y" is a ladder; applications chase X, learning aims at Y)
    target_role, deadline, weeks, north_star = _extract_role_and_deadline(ctx, goal)
    learn_role = north_star or target_role
    _log_step(0, "-", {}, {"target_role": target_role, "north_star_role": north_star,
                           "deadline": deadline, "weeks": weeks},
              "Parsed target role/timeline from the goal text (sensitive=False — "
              "no resume/profile data touched here).")

    # 2. create the goal + sync the profile (orchestrator bookkeeping about
    # its own plan, same line _store_plan_graph already draws — not gated)
    engine = GoalEngine(ctx.collab, events=ctx.events())
    gid = engine.create_goal(goal, domain="career", target_date=deadline)
    meta = {"target_role": target_role, "weeks": weeks}
    if north_star:
        meta["north_star_role"] = north_star
    ctx.collab.conn.execute(
        "UPDATE goals SET career_meta=? WHERE id=?", (json.dumps(meta), gid))
    ctx.collab.conn.commit()
    ctx.store.set_career_profile(ctx.user_id, target_role=target_role, deadline=deadline)
    _log_step(1, "create_goal", {"title": goal, "domain": "career"}, {"id": gid},
              f"New career goal: {target_role} by {deadline} ({weeks} weeks)"
              + (f", north star: {north_star}." if north_star else "."))

    # 3. skill gaps -> learning focuses (linked to this goal, same pattern
    # the Learning Feed already uses for goal-linked focuses). Gaps are
    # measured against the LADDER's destination (learn_role): you apply for
    # what you can win today, you learn toward where you're going.
    profile = ctx.store.get_career_profile(ctx.user_id) or {}
    gaps = _skill_gaps(ctx, learn_role, profile)
    from ..learning_feed.sensor import add_focus
    for topic in gaps:
        try:
            add_focus(ctx.collab.conn, ctx.user_id, topic, goal_id=gid)
        except Exception as exc:
            _log.warning("career template: add_focus(%r) failed: %s", topic, exc)
    _log_step(2, "-", {"target_role": learn_role}, {"gaps": gaps},
              "Skill gaps from real postings (job_search) vs current profile "
              "skills — degrades to a keyword diff if the LLM/search is unavailable.")

    # 4. weekly milestones (tracked progress, internal — not gated) + a
    # batched Plane task proposal (external send — IS gated, one approval
    # for the whole breakdown per the resolved design decision)
    milestones = _weekly_milestones(target_role, weeks, gaps,
                                    learn_role=learn_role)
    for title in milestones:
        try:
            engine.add_milestone(gid, title)
        except Exception as exc:
            _log.warning("career template: add_milestone(%r) failed: %s", title, exc)
    batch_reasoning = (f"Weekly milestone breakdown for '{target_role}' over "
                       f"{weeks} weeks, batched into one approval per the "
                       "CAREER AUTOPILOT design decision (atomic — approve "
                       "creates every task, reject creates none).")
    ctx._extras["agent_name"] = "career_goal_agent"
    ctx._extras["agent_reasoning"] = batch_reasoning
    ctx._extras["agent_dedup_key"] = f"career_milestones_{gid}"
    batch_result = tools.invoke(
        ctx, "plane_batch_create_tasks",
        {"tasks": [{"title": t} for t in milestones]}, actor="agent")
    _log_step(3, "plane_batch_create_tasks", {"tasks": milestones}, batch_result,
              batch_reasoning)

    # 5. portfolio analysis (Part 3): SHOWCASE/NEEDS WORK/GAPS classification
    # + vault note + its own gap-project batch approval. Degrades to a
    # {"error"/"skipped"} dict on any failure — never blocks the plan.
    from ..agents.reactive import portfolio_analyze
    portfolio_reasoning = ("Full portfolio pull + classification for the career "
                           "plan (against the ladder's destination role).")
    try:
        portfolio_result = portfolio_analyze(ctx.events(), ctx, target_role=learn_role,
                                             goal_id=gid)
    except Exception as exc:
        portfolio_result = {"error": str(exc)[:200]}
        _log.warning("career template: portfolio_analyze failed: %s", exc)
    _log_step(4, "portfolio_analyze", {"target_role": target_role}, portfolio_result,
              portfolio_reasoning)
    if isinstance(portfolio_result, dict):
        # portfolio_analyze proposes its own gap-project batch approval
        # internally (not surfaced as a top-level "pending" result the way
        # _log_step's is_pending check expects) — fold it in so
        # out["queued_approvals"] stays accurate.
        queued += int(portfolio_result.get("queued_approvals") or 0)

    portfolio_note = ""
    if isinstance(portfolio_result, dict) and portfolio_result.get("showcase"):
        portfolio_note = (f", portfolio classified ({len(portfolio_result['showcase'])} "
                          "showcase repo(s))")
    ladder_note = f" (north star: {north_star})" if north_star else ""
    summary = (f"Career plan for {target_role}{ladder_note} over {weeks} weeks: "
              f"goal created, {len(gaps)} learning focus(es) linked, "
              f"{len(milestones)} milestone(s) proposed as one batched "
              f"approval{portfolio_note}.")
    status = "completed"
    _persist_run(ctx, run_id, goal, plan, steps_log, summary, status)

    try:
        from ..events.store import CAREER_GOAL_SET
        payload = {"agent": "career_goal_agent", "goal_id": gid,
                  "target_role": target_role, "north_star_role": north_star,
                  "deadline": deadline, "reasoning": summary}
        eid = ctx.events().emit(CAREER_GOAL_SET, payload, source="orchestrator")
        from ..agents.reactive import _journal
        _journal(ctx, {"id": eid, "type": CAREER_GOAL_SET, "payload": payload,
                       "ts": None, "source": "orchestrator"})
    except Exception:
        pass   # fire-and-forget: the run row above is already the record

    return {"run_id": run_id, "goal": goal, "plan": plan,
            "plan_reasoning": "Career-shaped goal — used the career plan template.",
            "steps": steps_log, "summary": summary, "queued_approvals": queued,
            "status": status, "goal_id": gid}


def run_goal(ctx: JobCtx, goal: str, max_tool_calls: int = _MAX_TOOL_CALLS,
             run_id: str | None = None) -> dict:
    """When run_id is given (background mode), the caller pre-inserted a
    status='running' row — every exit path below replaces it, so a poller
    always sees the run finish (completed / failed)."""
    _ensure_table(ctx)
    run_id = run_id or uuid.uuid4().hex[:12]

    if _is_career_goal(goal):
        from .. import config
        if config.agent_enabled("career_goal"):
            try:
                return _run_career_template(ctx, goal, run_id)
            except Exception as exc:
                # Falls through to the generic planner below rather than
                # failing the whole run — the template degrades on its own
                # LLM-unavailable paths already; this is a last-resort net
                # for anything else (a DB error, a bad tool call).
                _log_career_template_failure(ctx, run_id, goal, exc)

    if ctx.llm is None:
        _persist_run(ctx, run_id, goal, [], [],
                     "No LLM provider is available right now.", "failed")
        return {"error": "No LLM provider is available right now."}
    catalog = _catalog()
    context = _context_block(ctx)

    # --- 1. plan -------------------------------------------------------------
    plan_resp = _gen(ctx, _PLAN_SYSTEM.format(catalog=catalog),
                     f"Recent activity:\n{context}\n\nGoal: {goal}")
    if not plan_resp or not isinstance(plan_resp.get("plan"), list) \
            or not plan_resp["plan"]:
        _persist_run(ctx, run_id, goal, [], [],
                     "Could not produce a plan — try rephrasing the goal.", "failed")
        return {"error": "Could not produce a plan — try rephrasing the goal."}
    plan = [str(s) for s in plan_resp["plan"][:_PLAN_MAX_STEPS]]
    plan_reasoning = str(plan_resp.get("reasoning") or "")
    task_ids = _store_plan_graph(ctx, run_id, goal, plan)

    # --- 2. execute ------------------------------------------------------------
    import time as _time
    from .. import tools
    deadline = _time.monotonic() + _TIME_BUDGET_S
    steps_log: list[dict] = []
    calls_used = 0
    for i, step in enumerate(plan):
        if _time.monotonic() > deadline:
            if i < len(task_ids):
                _mark_task(ctx, task_ids[i], step, "skipped (time budget)")
            continue
        step_outcome = "skipped (tool budget exhausted)"
        transcript = [f"Goal: {goal}", f"Plan: {json.dumps(plan)}",
                      f"Current step ({i + 1}/{len(plan)}): {step}"]
        for log in steps_log[-4:]:
            transcript.append(f"Earlier: {json.dumps(log, default=str)[:400]}")
        while calls_used < max_tool_calls and _time.monotonic() <= deadline:
            resp = _gen(ctx, _STEP_SYSTEM.format(catalog=catalog),
                        "\n".join(transcript) + "\nassistant:")
            if resp is None:
                step_outcome = "LLM unavailable"
                break
            if "step_done" in resp or "final" in resp:
                step_outcome = str(resp.get("step_done") or resp.get("final"))
                break
            tool_name = str(resp.get("tool") or "")
            args = resp.get("args") or {}
            reasoning = str(resp.get("reasoning") or f"step {i + 1}: {step}")
            ctx._extras["agent_name"] = "orchestrator"
            ctx._extras["agent_reasoning"] = reasoning
            # Found via manual testing: running an equivalent goal twice
            # ("cut spending 10%" vs "reduce spending by 10 percent") queued
            # two separate approvals for the IDENTICAL action. Dedup by
            # tool+args (not by goal phrasing) so a repeat proposal for the
            # same underlying change collapses into the existing pending
            # one; a fresh proposal is still allowed after rejection, since
            # create_approval's dedup only blocks pending/executed rows.
            ctx._extras["agent_dedup_key"] = (
                "orch_" + tool_name + "_" + hashlib.sha256(
                    json.dumps(args, sort_keys=True, default=str).encode()
                ).hexdigest()[:16])
            try:
                result = tools.invoke(ctx, tool_name, args, actor="agent")
                ok = True
            except Exception as exc:
                result = {"error": str(exc)}
                ok = False
            calls_used += 1
            entry = {"step": i, "tool": tool_name, "args": args,
                     "reasoning": reasoning, "ok": ok,
                     "queued": isinstance(result, dict) and result.get("status") == "pending",
                     "result": result}
            steps_log.append(entry)
            transcript.append(f"assistant: {json.dumps({'tool': tool_name, 'args': args})}")
            transcript.append("tool_result: " + json.dumps(result, default=str)[:2000])
        if i < len(task_ids):
            _mark_task(ctx, task_ids[i], step, step_outcome)

    # --- 3. summarize + persist + journal ---------------------------------------
    sum_resp = _gen(ctx, _SUMMARY_SYSTEM,
                    f"Goal: {goal}\nPlan: {json.dumps(plan)}\n"
                    f"Steps: {json.dumps(steps_log, default=str)[:4000]}")
    summary = str((sum_resp or {}).get("summary") or
                  f"Ran {len(plan)} step(s), {calls_used} tool call(s).")
    queued = sum(1 for s in steps_log if s.get("queued"))
    status = "completed" if calls_used or steps_log else "planned_only"
    _persist_run(ctx, run_id, goal, plan, steps_log, summary, status)

    try:
        payload = {"agent": "orchestrator", "summary": f"Goal run: {goal[:80]}",
                   "reasoning": plan_reasoning or summary, "run_id": run_id,
                   "plan": plan, "queued_approvals": queued}
        eid = ctx.events().emit("agent.goal_planned", payload, source="orchestrator")
        from ..agents.reactive import _journal
        _journal(ctx, {"id": eid, "type": "agent.goal_planned",
                       "payload": payload, "ts": None, "source": "orchestrator"})
    except Exception:
        pass   # fire-and-forget: the run row above is already the record

    return {"run_id": run_id, "goal": goal, "plan": plan,
            "plan_reasoning": plan_reasoning, "steps": steps_log,
            "summary": summary, "queued_approvals": queued, "status": status}
