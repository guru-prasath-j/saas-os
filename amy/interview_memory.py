"""Interview Memory (CAREER AUTOPILOT Phase F, final phase) — a manually-
logged interview journal with pattern analysis over time, NOT a passive
detection system. The user reports what happened (questions asked,
self-assessment, outcome); this module's value-add is structuring that
data and finding patterns across entries — it never invents or infers
interview content the user didn't provide, whether logged via the
structured route or the LLM-assisted chat path below.

`interview_debrief_check` (amy/agents/reactive.py, Part 5E) already
prompts exactly once per career-linked calendar event ending — a vault-
note skeleton + one notification, deduped via a prefs-table guard. There
is no second "an interview just happened" detector here; that
function's notification body now also points at log_interview_from_chat/
POST /api/career/interviews as the structured destination, but its own
once-per-event dedup and vault-note behavior are untouched.

application_id links to the existing `applications` table. When given,
`company` is DERIVED from the linked application's posting at write
time (never independently trusted) — `applications` itself has no
company column, it's always resolved via get_posting(), same pattern
career_inbound.py already uses. An interview_id with no application_id
(a referral chat, an early informational round not yet a formal
application) still gets a company-only row from the caller's own text.

Tiering: log_interview()/log_interview_from_chat() call submit_action(
ctx, tier=1, ...) DIRECTLY, bypassing tools.invoke(actor="agent")/
AGENT_GATE's env-driven _tier_for("write") policy (default tier 2) — the
same established pattern amy/life/habits.py::_complete() and amy/
career_sprint.py::generate_sprint() already use for "internal,
reversible, no-external-system action that still needs a record+notify
step." This is the user's own self-report, not an external action, so
it doesn't need tier-2 approval — auto-executed + notified instead.

Skill-gap cross-referencing (interview_patterns()'s linked_skill_gaps)
is READ-ONLY against the EXISTING shared graph.db Phase B already
populates (amy/career_graph.py's skill: node-id convention) — a
weakness tag that doesn't exactly match an existing skill: node stays
unlinked, never fuzzy-guessed or backfilled.
"""
from __future__ import annotations

import re

_ROUND_TYPES = ("phone_screen", "technical", "system_design", "behavioral",
                "onsite", "other")
_OUTCOMES = ("strong", "ok", "weak")


# ---------------------------------------------------------------------------
# Skill-graph cross-reference — read-only
# ---------------------------------------------------------------------------

def _linked_skill_gap(ctx, tag: str) -> str | None:
    from .career_graph import _graph_path
    from .knowledge_graph.store import GraphStore

    node_id = f"skill:{tag.strip().lower()}"
    g = GraphStore(_graph_path(ctx))
    try:
        node = g.get_node(node_id)
    finally:
        g.close()
    return node["label"] if node else None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log_interview(ctx, application_id: str | None = None, company: str = "",
                  round_type: str = "other", questions: list[str] | None = None,
                  self_assessed_outcome: str = "ok",
                  weakness_tags: list[str] | None = None, notes: str = "") -> dict:
    from .automation.executors import submit_action

    if round_type not in _ROUND_TYPES:
        raise ValueError(f"unknown round_type {round_type!r} — must be one of {_ROUND_TYPES}")
    if self_assessed_outcome not in _OUTCOMES:
        raise ValueError(f"unknown self_assessed_outcome {self_assessed_outcome!r} "
                         f"— must be one of {_OUTCOMES}")

    if application_id:
        app = ctx.store.get_application(ctx.user_id, application_id)
        if app is None:
            return {"error": f"no application {application_id!r} on file"}
        posting = ctx.store.get_posting(ctx.user_id, app["posting_id"]) or {}
        company = posting.get("company") or company   # derived, not independently trusted

    questions = questions or []
    weakness_tags = weakness_tags or []
    linked = sorted({tag for tag in weakness_tags if _linked_skill_gap(ctx, tag)})

    result = submit_action(
        ctx, tier=1, action_type="interview_log_create",
        title=f"Interview logged: {company or 'unspecified company'} ({round_type})",
        body=f"Self-assessed outcome: {self_assessed_outcome}. "
            f"{len(questions)} question(s), {len(weakness_tags)} weakness tag(s)"
            + (f" ({', '.join(linked)} linked to a real skill gap)." if linked else "."),
        payload={"application_id": application_id, "company": company,
                "round_type": round_type, "questions": questions,
                "self_assessed_outcome": self_assessed_outcome,
                "weakness_tags": weakness_tags, "notes": notes},
        source="interview_memory",
        reasoning="User-reported interview debrief — logged as-is, no content inferred.",
        risk="write", affected_entity=f"application_id={application_id or ''}")

    try:
        from .events.factory import get_events
        from .events.store import CAREER_INTERVIEW_LOGGED
        get_events(ctx.user_id, ctx.collab, ctx=ctx).emit(
            CAREER_INTERVIEW_LOGGED,
            {"application_id": application_id, "company": company,
            "round_type": round_type, "self_assessed_outcome": self_assessed_outcome},
            source="interview_memory")
    except Exception:
        pass

    return result


_DEBRIEF_SYSTEM = (
    "You structure a user's freeform interview debrief into a fixed schema. "
    "NEVER invent a question, weakness, or detail the user didn't actually "
    "say — only reorganize what's given. If the outcome isn't stated, use "
    "'ok'. Respond with EXACTLY ONE JSON object: {\"round_type\": "
    "\"phone_screen|technical|system_design|behavioral|onsite|other\", "
    "\"questions\": [\"...\"], \"self_assessed_outcome\": \"strong|ok|weak\", "
    "\"weakness_tags\": [\"...\"], \"notes\": \"...\"}"
)


def _resolve_application_by_company(ctx, company: str) -> str | None:
    """Best-effort match against the user's own in-progress applications —
    same _company_token heuristic amy/career_inbound.py and interview_
    debrief_check already use. No match -> honestly unlinked, never
    guessed."""
    from .career_inbound import _company_token

    token = _company_token(company)
    if not token:
        return None
    for app in ctx.store.list_applications(ctx.user_id):
        if app["status"] not in ("interview", "offer"):
            continue
        posting = ctx.store.get_posting(ctx.user_id, app["posting_id"]) or {}
        if token in _company_token(posting.get("company") or ""):
            return app["id"]
    return None


def log_interview_from_chat(ctx, company: str, description: str) -> dict:
    from .agents.reactive import _get_llm

    fallback = {"round_type": "other", "questions": [], "self_assessed_outcome": "ok",
               "weakness_tags": [], "notes": description}
    parsed = fallback
    llm = _get_llm(ctx)
    if llm is not None:
        try:
            import json as _json

            text, provider = llm.generate(
                _DEBRIEF_SYSTEM,
                f"Company: {company}\nDebrief: {description}", sensitive=True)
            if provider != "template":
                m = re.search(r"\{.*\}", text, re.DOTALL)
                if m:
                    candidate = _json.loads(m.group(0))
                    if candidate.get("round_type") in _ROUND_TYPES:
                        parsed = {
                            "round_type": candidate["round_type"],
                            "questions": [str(q) for q in (candidate.get("questions") or [])],
                            "self_assessed_outcome": candidate.get("self_assessed_outcome")
                                if candidate.get("self_assessed_outcome") in _OUTCOMES else "ok",
                            "weakness_tags": [str(t) for t in (candidate.get("weakness_tags") or [])],
                            "notes": str(candidate.get("notes") or description),
                        }
        except Exception:
            pass   # degrade to the honest fallback — never block the log

    application_id = _resolve_application_by_company(ctx, company)
    return log_interview(ctx, application_id=application_id, company=company, **parsed)


# ---------------------------------------------------------------------------
# Pattern analysis — retrospective aggregation, never forecasting
# ---------------------------------------------------------------------------

def interview_patterns(ctx) -> dict:
    logs = ctx.store.list_interview_logs(ctx.user_id)
    total = len(logs)

    tag_counts: dict[str, int] = {}
    for log in logs:
        for tag in log.get("weakness_tags") or []:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    recurring_weaknesses = [
        {"tag": tag, "count": count,
        "pct_of_interviews": round(100.0 * count / total, 1) if total else 0.0}
        for tag, count in sorted(tag_counts.items(), key=lambda kv: kv[1], reverse=True)
    ]

    outcome_by_round_type: dict[str, dict[str, int]] = {}
    for log in logs:
        bucket = outcome_by_round_type.setdefault(
            log["round_type"], {o: 0 for o in _OUTCOMES})
        bucket[log["self_assessed_outcome"]] += 1

    linked_skill_gaps = sorted({
        linked for tag in tag_counts
        if (linked := _linked_skill_gap(ctx, tag)) is not None
    })

    return {"total_logged": total, "recurring_weaknesses": recurring_weaknesses,
           "outcome_by_round_type": outcome_by_round_type,
           "linked_skill_gaps": linked_skill_gaps}


def interview_weakness_report(ctx) -> dict:
    """Thin wrapper over interview_patterns() — never recomputes."""
    patterns = interview_patterns(ctx)
    if not patterns["recurring_weaknesses"]:
        summary = "No recurring weaknesses identified yet — log a few more interviews."
    else:
        top = patterns["recurring_weaknesses"][0]
        summary = (f"Most common weakness: '{top['tag']}' in {top['count']} of "
                  f"{patterns['total_logged']} logged interview(s) "
                  f"({top['pct_of_interviews']}%).")
    return {"summary": summary, "recurring_weaknesses": patterns["recurring_weaknesses"],
           "linked_skill_gaps": patterns["linked_skill_gaps"]}
