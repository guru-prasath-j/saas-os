"""Future-self ledger (CONTEXT_PLAN C7) — preference drift from decisions.

Every approve/reject/expire already lands in the approvals ledger. Monthly,
this job reads six months of those decisions and surfaces the patterns the
user can't see day-to-day:

  always-reject   — you reject nearly everything of some kind: the proposer
                    is mis-tuned; fix its thresholds or disable the job
  always-approve  — you rubber-stamp some kind every time: it's a candidate
                    for tier 1 (auto + notify) instead of the inbox
  ignored         — proposals of some kind mostly expire unreviewed: the
                    inbox is nagging about something you don't care about

Local statistics only — no LLM, no content analysis, just your own decisions
reflected back. One notification per month, one insight per signal."""
from __future__ import annotations

import datetime as _dt

LOOKBACK_DAYS = 180
MIN_DECISIONS = 3          # don't infer a preference from one-off noise
REJECT_RATE = 0.6
APPROVE_STREAK = 5
EXPIRE_RATE = 0.5


def _signals(rows: list[dict]) -> list[dict]:
    """rows: (action_type, source, status) decided approvals → drift signals."""
    groups: dict[tuple, dict[str, int]] = {}
    for r in rows:
        key = (r["action_type"], r["source"] or "")
        g = groups.setdefault(key, {"executed": 0, "rejected": 0, "expired": 0})
        status = "executed" if r["status"] in ("executed", "auto_executed") \
            else r["status"]
        if status in g:
            g[status] += 1

    signals = []
    for (action, source), g in sorted(groups.items()):
        decided = g["executed"] + g["rejected"]
        total = decided + g["expired"]
        label = f"'{action}' from {source or 'unknown'}"
        if decided >= MIN_DECISIONS and g["rejected"] / decided >= REJECT_RATE:
            signals.append({
                "kind": "always_reject", "action_type": action, "source": source,
                "summary": f"You reject most {label} proposals",
                "detail": (f"{g['rejected']} of {decided} decided were rejected. "
                           "The proposer looks mis-tuned — adjust its thresholds "
                           "or disable that job instead of re-rejecting forever.")})
        elif g["executed"] >= APPROVE_STREAK and g["rejected"] == 0:
            signals.append({
                "kind": "always_approve", "action_type": action, "source": source,
                "summary": f"You always approve {label}",
                "detail": (f"{g['executed']} approvals, zero rejections. "
                           "Candidate for tier 1 (auto + notify) — the inbox "
                           "step is adding friction, not oversight.")})
        if total >= MIN_DECISIONS and g["expired"] / total >= EXPIRE_RATE:
            signals.append({
                "kind": "ignored", "action_type": action, "source": source,
                "summary": f"{label} proposals mostly expire unreviewed",
                "detail": (f"{g['expired']} of {total} expired without a "
                           "decision. Either they don't matter (disable the "
                           "job) or they deserve a better surface.")})
    return signals


def preference_drift(ctx) -> dict:
    since = (_dt.datetime.now(_dt.timezone.utc)
             - _dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    rows = [dict(r) for r in ctx.collab.conn.execute(
        "SELECT action_type, source, status FROM approvals"
        " WHERE status IN ('executed','auto_executed','rejected','expired')"
        " AND created_at >= ?", (since,)).fetchall()]
    signals = _signals(rows)

    events = ctx.events()
    for s in signals:
        try:
            events.emit("agent.insight",
                        {"agent": "drift", "summary": s["summary"],
                         "reasoning": s["detail"], "kind": s["kind"],
                         "action_type": s["action_type"],
                         "source_job": s["source"]},
                        source="drift_agent")
        except Exception:
            pass

    if signals:
        ns = ctx.notify_store()
        month = _dt.date.today().strftime("%Y-%m")
        ref = f"drift_{month}"
        if not ns.exists_today("preference_drift", ref):
            lines = "\n".join(f"• {s['summary']}: {s['detail']}"
                              for s in signals[:5])
            ns.create(type="preference_drift",
                      title=f"What your decisions say ({month})",
                      body=(f"{len(rows)} decisions in the last "
                            f"{LOOKBACK_DAYS} days.\n{lines}"),
                      priority="normal",
                      related_entity={"entity_type": "drift", "id": ref})
    return {"decisions": len(rows), "signals": len(signals)}
