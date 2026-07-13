"""Portfolio Builder (CAREER AUTOPILOT Phase D, part 1) — persists the
SHOWCASE/NEEDS_WORK/NOT_RELEVANT classification `amy/agents/reactive.py::
_classify_repos()` already computes (previously reaching only a vault
note as formatted text — confirmed via `amy/career_graph.py`'s own
module docstring before this phase), and proposes targeted per-repo
update suggestions on real new GitHub activity.

Persistence: `persist_classification()` is called from `portfolio_
analyze()` right after its existing `_classify_repos()` call — the SAME
classify pass, not a second one. `portfolio_analyze()`'s existing
behavior (vault note, gap-project batch approval, master-resume
evolution proposal) is untouched; this is one additive upsert per repo.

Activity trigger: `GitHubSensor` (amy/connectors/sensors.py) has no
commit/release-level polling — confirmed reading it in full — only PR
review-requests/status-changes and issue-assignment. The only real,
already-live GitHub read is `portfolio_repo_list` (repo metadata
including, per standard GitHub API shape, `pushed_at`/`updated_at`).
`scan_github_activity()` compares each persisted repo's live push
timestamp against a stored cursor via the SAME `sensor_seen_state()`/
`mark_sensor_seen()` mechanism GitHubSensor already uses for PR/issue
diffing (`connector_sensor_seen` table, sensor name "portfolio_repo_
activity") — real activity detection, not fabricated per-commit
granularity that doesn't exist as a signal in this codebase.

Every proposal (manual or scan-triggered) calls submit_action(ctx,
tier=2, ...) DIRECTLY, not through tools.invoke(actor="agent") — RISK_
WRITE + actor="human" would otherwise execute immediately (quirk 15),
which would let a UI-triggered refresh save straight to portfolio_items
with no review step. This project's own framing for this phase is
stricter than the usual internal-write default ("no exceptions") — same
fixed-tier-2 pattern AML's escalate_case/generate_sar_draft and Loan's
apply_for_loan already use for irreversible/public-facing content.

No auto-publishing anywhere: propose_portfolio_update only ever changes
Amy's own local portfolio_items.why/bullets suggestion — nothing is
written back to GitHub (no such API is wired up, and wouldn't belong in
an approval-gated local suggestion anyway).
"""
from __future__ import annotations

import datetime as _dt

_ACTIVITY_SENSOR = "portfolio_repo_activity"


def _pushed_at(repo: dict) -> str | None:
    for k in ("pushed_at", "updated_at", "last_activity_at"):
        v = repo.get(k)
        if v:
            return str(v)
    return None


def _repo_name(repo: dict) -> str:
    return str(repo.get("name") or repo.get("full_name") or "").strip()


def persist_classification(ctx, target_role: str, showcase: list[dict],
                           needs_work: list[dict], not_relevant: list[dict],
                           entries_by_repo: dict[str, dict] | None = None) -> int:
    """Upserts one portfolio_items row per classified repo. entries_by_repo
    (optional) maps repo name -> {"why", "bullets"} from portfolio_
    analyze()'s existing narrative entries (LLM or fallback) — when
    absent (e.g. a needs_work/not_relevant repo, which never gets a
    narrative entry), why/bullets stay empty rather than fabricated."""
    entries_by_repo = entries_by_repo or {}
    n = 0
    for classification, repos in (("showcase", showcase),
                                  ("needs_work", needs_work),
                                  ("not_relevant", not_relevant)):
        for r in repos:
            name = _repo_name(r)
            if not name:
                continue
            entry = entries_by_repo.get(name) or {}
            pushed_at = _pushed_at(r)
            ctx.store.upsert_portfolio_item(
                ctx.user_id, name, classification,
                matched_keywords=r.get("_matched_keywords") or [],
                missing=r.get("_missing") or [],
                why=entry.get("why", ""), bullets=entry.get("bullets") or [],
                target_role=target_role, last_pushed_at=pushed_at)
            # Seed the activity cursor at classification time — otherwise
            # scan_github_activity's first-ever run would see "never seen"
            # (sensor_seen_state returns None) and treat every repo as new
            # activity, proposing for all of them immediately instead of
            # only on a genuine change since this classification.
            if pushed_at:
                ctx.store.mark_sensor_seen(_ACTIVITY_SENSOR, name, pushed_at)
            n += 1
    return n


def propose_portfolio_update(ctx, repo_name: str, why: str, bullets: list[str],
                             source: str = "portfolio_builder") -> dict:
    """Always tier 2 via a direct submit_action call (see module
    docstring) — the approval body shows current vs. proposed why/bullets
    so a human reviews the actual diff, same shape as the master-resume
    evolution proposal (_propose_resume_evolution)."""
    from .automation.executors import submit_action

    current = ctx.store.get_portfolio_item(ctx.user_id, repo_name) or {}
    body = (f"Current: {current.get('why', '(none yet)')}\n"
           f"Proposed: {why}\n\n"
           f"Bullets:\n" + "\n".join(f"- {b}" for b in bullets))
    today = _dt.date.today().isoformat()
    return submit_action(
        ctx, tier=2, action_type="portfolio_item_update",
        title=f"Portfolio update: {repo_name}",
        body=body,
        payload={"repo_name": repo_name, "why": why, "bullets": bullets},
        source=source,
        dedup_key=f"portfolio_update_{repo_name}_{today}",
        reasoning=f"New activity or refreshed classification for '{repo_name}' "
                 "— proposing an updated description/bullets, never auto-applied.",
        risk="write", affected_entity=f"repo={repo_name}")


def scan_github_activity(ctx) -> dict:
    """Job-driven trigger: re-checks every persisted portfolio_items row's
    live pushed_at against the stored cursor; on a real change, re-
    classifies that ONE repo (not a full portfolio_analyze() re-run, which
    would re-propose gap projects / resume evolution too) and proposes a
    refreshed why/bullets. Honest {"skipped": ...} with no GitHub
    connector on file, mirroring GitHubSensor's own early return."""
    from . import tools
    from .agents.reactive import _classify_repos
    from .connectors.mcp_call import find_connector_row

    if find_connector_row(ctx.user_id, "github") is None:
        return {"skipped": "no github connector"}

    items = ctx.store.list_portfolio_items(ctx.user_id)
    if not items:
        return {"skipped": "no persisted portfolio items yet — run portfolio analysis first"}

    try:
        repo_out = tools.invoke(ctx, "portfolio_repo_list", {}, actor="agent")
        repos = repo_out.get("repos") or []
    except Exception as exc:
        return {"error": f"portfolio_repo_list failed: {str(exc)[:200]}"}
    live_by_name = {_repo_name(r): r for r in repos if _repo_name(r)}

    proposed = 0
    checked = 0
    for item in items:
        repo = live_by_name.get(item["repo_name"])
        if repo is None:
            continue
        checked += 1
        live_pushed = _pushed_at(repo)
        last_seen = ctx.store.sensor_seen_state(_ACTIVITY_SENSOR, item["repo_name"])
        if live_pushed is None or live_pushed == last_seen:
            continue

        keywords = set(item.get("matched_keywords") or [])
        if keywords:
            showcase, needs_work, not_relevant = _classify_repos([repo], keywords)
            classification = ("showcase" if showcase else
                              "needs_work" if needs_work else "not_relevant")
            matched = (showcase + needs_work + not_relevant)[0].get("_matched_keywords") or []
            missing = (showcase + needs_work + not_relevant)[0].get("_missing") or []
        else:
            classification, matched, missing = item["classification"], [], item.get("missing") or []

        why = (f"Refreshed after new activity on '{item['repo_name']}' "
              f"(pushed {live_pushed}).")
        bullets = [f"Recently updated — technologies: {repo.get('language') or 'multiple'}",
                  f"Matched keywords: {', '.join(matched) or 'none'}"]
        ctx.store.upsert_portfolio_item(
            ctx.user_id, item["repo_name"], classification,
            matched_keywords=matched, missing=missing,
            why=item.get("why", ""), bullets=item.get("bullets") or [],
            target_role=item.get("target_role", ""), last_pushed_at=live_pushed)
        result = propose_portfolio_update(ctx, item["repo_name"], why, bullets,
                                          source="portfolio_activity_scan")
        if result.get("status") == "pending":
            proposed += 1
        ctx.store.mark_sensor_seen(_ACTIVITY_SENSOR, item["repo_name"], live_pushed)

    return {"checked": checked, "proposed": proposed}
