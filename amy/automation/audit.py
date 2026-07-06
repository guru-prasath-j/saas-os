"""Audit export (Phase R7A-6) — one regulator-style report of everything the
machine did in a period: events, automation runs, approvals with reasoning
and outcomes, decision journal, and (from R7A-1) values-screening flags.

Everything is read from tables that already exist; this module only joins
and shapes. The metadata block documents LLM routing so a reviewer can see
exactly which providers could have seen which class of data.
"""
from __future__ import annotations

import datetime as _dt
import json


def _iso_or(default_days_ago: int, value: str | None) -> str:
    if value:
        return value if "T" in value else f"{value}T00:00:00"
    return (_dt.datetime.now(_dt.timezone.utc)
            - _dt.timedelta(days=default_days_ago)).isoformat()


def _llm_routing_doc(ctx) -> dict:
    """Which providers can see what — part of the report metadata."""
    from .. import config
    local_only = False
    try:
        row = ctx.collab.conn.execute(
            "SELECT value FROM prefs WHERE key='llm_local_only'").fetchone()
        local_only = bool(row and str(row["value"]) == "1")
    except Exception:
        pass
    return {
        "general_provider_order": config.GENERAL_PROVIDER_ORDER,
        "sensitive_data_rule": ("data matching sensitivity rules (e.g. tax-ID "
                                "patterns, 'sensitive' tags) is routed to the "
                                "LOCAL model only (Ollama); cloud providers "
                                "never receive it"),
        "user_local_only_flag": local_only,
        "effective_routing": ("ALL calls local-only for this user"
                              if local_only else
                              "general → provider order above; sensitive → local only"),
    }


def build_audit_report(ctx, since: str | None = None,
                       until: str | None = None,
                       limit_per_section: int = 1000) -> dict:
    since_iso = _iso_or(30, since)
    until_iso = _iso_or(0, until) if until else _dt.datetime.now(_dt.timezone.utc).isoformat()
    conn = ctx.collab.conn

    # --- events (agent activity + finance/custodial provenance trail) -------
    ev_rows = conn.execute(
        "SELECT id, ts, type, payload, source FROM events"
        " WHERE ts>=? AND ts<=? ORDER BY ts DESC LIMIT ?",
        (since_iso, until_iso, limit_per_section)).fetchall()
    events = []
    for r in ev_rows:
        p = json.loads(r["payload"] or "{}")
        events.append({"id": r["id"], "ts": r["ts"], "type": r["type"],
                       "source": r["source"],
                       "agent": p.get("agent"),
                       "reasoning": p.get("reasoning"),
                       "summary": p.get("summary"),
                       "payload": p})

    # --- automation runs -----------------------------------------------------
    runs = [dict(r) for r in conn.execute(
        "SELECT id, job_name, started_at, finished_at, status, detail"
        " FROM automation_runs WHERE started_at>=? AND started_at<=?"
        " ORDER BY started_at DESC LIMIT ?",
        (since_iso, until_iso, limit_per_section)).fetchall()]
    for r in runs:
        r["detail"] = json.loads(r["detail"] or "{}")

    # --- approvals (proposals + outcomes, with reasoning) --------------------
    approvals = [dict(r) for r in conn.execute(
        "SELECT id, created_at, decided_at, tier, action_type, title,"
        " reasoning, risk, affected_entity, status, source, payload, result,"
        " expires_at FROM approvals WHERE created_at>=? AND created_at<=?"
        " ORDER BY created_at DESC LIMIT ?",
        (since_iso, until_iso, limit_per_section)).fetchall()]
    for a in approvals:
        a["payload"] = json.loads(a["payload"] or "{}")
        a["result"] = json.loads(a["result"]) if a["result"] else None

    # --- decision journal (incl. approve/reject records) ---------------------
    decisions = [dict(r) for r in conn.execute(
        "SELECT id, ts, title, reason, domain, confidence, outcome, status"
        " FROM decisions WHERE ts>=? AND ts<=? ORDER BY ts DESC LIMIT ?",
        (since_iso, until_iso, limit_per_section)).fetchall()]

    # --- values-screening flags (populated by R7A-1; empty until then) -------
    screening_flags: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT * FROM screening_flags WHERE created_at>=? AND created_at<=?"
            " ORDER BY created_at DESC LIMIT ?",
            (since_iso, until_iso, limit_per_section)).fetchall()
        screening_flags = [dict(r) for r in rows]
    except Exception:
        pass   # table ships with the values engine (R7A-1)

    agent_events = [e for e in events if (e["type"] or "").startswith("agent.")]
    return {
        "metadata": {
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "period": {"from": since_iso, "to": until_iso},
            "user_id": ctx.user_id,
            "llm_routing": _llm_routing_doc(ctx),
            "disclaimer": ("Automated actions are recorded with the proposing "
                           "agent's reasoning. Obligation/tax figures anywhere "
                           "in this report are estimates, not professional "
                           "advice."),
            "counts": {
                "events": len(events),
                "agent_events": len(agent_events),
                "automation_runs": len(runs),
                "approvals": len(approvals),
                "approvals_pending": sum(1 for a in approvals if a["status"] == "pending"),
                "approvals_rejected": sum(1 for a in approvals if a["status"] == "rejected"),
                "decisions": len(decisions),
                "screening_flags": len(screening_flags),
            },
        },
        "agent_activity": agent_events,
        "events": events,
        "automation_runs": runs,
        "approvals": approvals,
        "decisions": decisions,
        "screening_flags": screening_flags,
    }
