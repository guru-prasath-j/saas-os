"""Reactive agents — subscribers that act the moment data arrives (Phase R2).

Registered in the amy/events/triggers.py style onto whichever EventStore
instance is about to emit (both _emit_fin in the finance router and
JobCtx.events() in the automation layer call register_reactive_agents), so
reactions fire no matter which code path imported the data.

Agents:
  budget       — after an import OR a single manual transaction add,
                 re-checks caps vs actual spend (scoped to the affected
                 category on a manual add, so entering one transaction
                 doesn't re-notify about unrelated categories)
  subscription — after an import, proactively detects new recurring charges
                 and proposes them through the approval queue
  compliance   — after a ledger post, runs compliance suggestions for
                 'close'-tracked business entities (advisory rows only)
  learning     — after a learning-feed refresh, proposes a goal for a
                 topic that's trending up and isn't goal-linked yet, and
                 nudges (advisory only) a goal-linked focus with zero
                 engagement after repeated refreshes
  pr_task      — (CONNECTOR COMPLETION Part 2) a GitHub PR that needs review
                 or just went changes-requested proposes a Plane task
                 (EXTERNAL tool, always tier 2, deduped per PR)
  meeting_prep — (CONNECTOR COMPLETION Part 2) no event subscription — see
                 meeting_prep_check(), driven by the meeting_prep_scan job.
                 Read-only: writes a vault note + agent.insight, never a
                 write proposal

Rules honored here:
  - every insight/action carries an explicit reasoning string
  - kill switches: AMY_AGENT_BUDGET / _SUBSCRIPTION / _COMPLIANCE / _PR_TASK /
    _MEETING_PREP (default ON)
  - agent errors are reported as agent.error events, never raised into the
    emitting route (the bus additionally retries once + dead-letters)
  - all proposals go through the tool registry with actor="agent", so the
    R3 approval gate applies; nothing here writes user data directly
  - journaling via MemoryWriter.log_event is idempotent on event id
"""
from __future__ import annotations

import datetime as _dt

from .. import config

_BUDGET_WARN_PCT = 0.90       # insight when a category crosses 90% of its cap
_SUB_MIN_CONFIDENCE = 0.75    # propose only confident subscription candidates
_IMPORT_EVENTS = ("finance.gmail_synced", "finance.csv_imported",
                  "finance.pdf_imported")
# Anything that adds transactions to the ledger — bulk imports plus a single
# manually-entered transaction (finance.transaction_added has no "imported"
# count; its presence alone means one row was just added).
_TRANSACTION_EVENTS = _IMPORT_EVENTS + ("finance.transaction_added",)


def _get_llm(ctx):
    """Lazy LLM: route-driven emissions build ctx without an LLM; agents that
    need one construct it once and cache it on the ctx."""
    if ctx.llm is not None:
        return ctx.llm
    cached = ctx._extras.get("lazy_llm")
    if cached is not None:
        return cached
    try:
        from ..llm import LLMRouter
        from ..automation.store import TrackedLLM
        llm = TrackedLLM(LLMRouter(use_global_keys=True), ctx.store,
                         purpose="reactive_agent")
    except Exception:
        llm = None
    ctx._extras["lazy_llm"] = llm
    return llm


def _journal(ctx, ev: dict) -> None:
    """Idempotent vault journaling of an agent event. Never raises."""
    try:
        from ..memory.writer import MemoryWriter
        from ..saas import tenancy
        # linked cloud vault, not the internal folder — otherwise agent notes
        # never sync AND are invisible to memory recall (which reads tenancy)
        vault = tenancy.resolve_vault_dir(ctx.user_id)
        vault.mkdir(parents=True, exist_ok=True)
        MemoryWriter(vault).log_event(ev)
    except Exception:
        pass   # journaling is best-effort; the event row is the record


def _emit_insight(events, ctx, agent: str, summary: str, reasoning: str,
                  extra: dict | None = None) -> None:
    payload = {"agent": agent, "summary": summary, "reasoning": reasoning}
    payload.update(extra or {})
    eid = events.emit("agent.insight", payload, source=f"{agent}_agent")
    _journal(ctx, {"id": eid, "type": "agent.insight", "payload": payload,
                   "ts": None, "source": f"{agent}_agent"})


def _report_error(events, agent: str, exc: Exception) -> None:
    try:
        events.emit("agent.error", {"agent": agent, "error": str(exc)[:400],
                                    "reasoning": "handler raised; see error"},
                    source=f"{agent}_agent")
    except Exception:
        pass   # the bus dead-letters the original failure regardless


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

def _budget_agent(events, ctx):
    def on_transaction_activity(ev):
        try:
            p = ev.get("payload") or {}
            is_manual_add = ev.get("type") == "finance.transaction_added"
            if not is_manual_add and not p.get("imported"):
                return   # bulk import brought in nothing new
            fe = ctx.open_finance()
            try:
                statuses = fe.budget_status()
            finally:
                fe.close()
            ns = ctx.notify_store()
            # A manual single add only needs to re-check the ONE category
            # that changed — otherwise entering one Food transaction would
            # re-notify about every other already-over-budget category too.
            # A bulk import may have touched several, so check them all.
            target_category = p.get("category") if is_manual_add else None
            for b in statuses:
                if not b.get("limit"):
                    continue
                if target_category and b["category"] != target_category:
                    continue
                spent, limit = b["spent"], b["limit"]
                if spent < limit * _BUDGET_WARN_PCT:
                    continue
                over = spent > limit
                state = "over budget" if over else f"at {spent / limit:.0%} of budget"
                trigger = ("You just added a transaction" if is_manual_add
                           else f"Import event {ev.get('id')} added transactions")
                reasoning = (f"{trigger}; '{b['category']}' now {state} "
                             f"(spent {spent:,.0f} of {limit:,.0f}).")
                summary = f"{b['category']} {state}"
                _emit_insight(events, ctx, "budget", summary, reasoning,
                              {"category": b["category"], "spent": spent,
                               "limit": limit, "source_event_id": ev.get("id")})
                ref = f"agent_budget_{b['category']}"
                if not ns.exists_today("agent_budget_check", ref):
                    ns.create(type="agent_budget_check",
                              title=f"Budget check: {summary}",
                              body=reasoning,
                              priority="high" if over else "normal",
                              related_entity={"id": ref, "entity_type": "budget",
                                              "category": b["category"]})
        except Exception as exc:
            _report_error(events, "budget", exc)

    def on_place_entered(ev):
        # Spend-aware geofencing (CONTEXT_PLAN C2): walking into a place whose
        # kind maps to a nearly-exhausted budget warns BEFORE the purchase —
        # the one moment a budget alert can actually change the outcome.
        try:
            p = ev.get("payload") or {}
            name = (p.get("name") or "").strip()
            kind = (p.get("kind") or "").strip().lower()
            place_id = p.get("place_id") or ""
            if not (name or kind):
                return
            place_tokens = _errand_tokens(kind + " " + name)
            aliased = set(place_tokens)
            for tok in place_tokens:
                aliased |= _errand_tokens(_KIND_BUDGET_ALIASES.get(tok, ""))
            fe = ctx.open_finance()
            try:
                statuses = fe.budget_status()
            finally:
                fe.close()
            ns = ctx.notify_store()
            for b in statuses:
                limit = b.get("limit")
                if not limit or not (aliased & _errand_tokens(b["category"])):
                    continue
                spent = b["spent"]
                if spent < limit * _BUDGET_WARN_PCT:
                    continue
                pct = spent / limit
                reasoning = (f"You just arrived at '{name}'"
                             f"{f' ({kind})' if kind else ''}, which maps to "
                             f"your '{b['category']}' budget — already at "
                             f"{pct:.0%} ({spent:,.0f} of {limit:,.0f}). "
                             "Flagged before you spend, not after.")
                _emit_insight(events, ctx, "budget",
                              f"{b['category']} at {pct:.0%} — you're at {name}",
                              reasoning,
                              {"category": b["category"], "spent": spent,
                               "limit": limit, "place_id": place_id,
                               "source_event_id": ev.get("id")})
                ref = f"spendwarn_{b['category']}_{place_id}"
                if not ns.exists_today("spend_caution", ref):
                    ns.create(type="spend_caution",
                              title=f"Careful at {name}",
                              body=reasoning,
                              priority="high" if spent > limit else "normal",
                              related_entity={"id": ref, "entity_type": "budget",
                                              "category": b["category"],
                                              "place_id": place_id})
        except Exception as exc:
            _report_error(events, "budget", exc)

    for etype in _TRANSACTION_EVENTS:
        events.subscribe(etype, on_transaction_activity)
    events.subscribe("context.place_entered", on_place_entered)


def _subscription_agent(events, ctx):
    def on_import(ev):
        try:
            if not (ev.get("payload") or {}).get("imported"):
                return
            from ..finance.subscription_detect import detect_subscriptions
            from .. import tools
            fe = ctx.open_finance()
            try:
                candidates = detect_subscriptions(fe, _get_llm(ctx))
            finally:
                fe.close()
            for c in candidates:
                if (c.get("confidence") or 0) < _SUB_MIN_CONFIDENCE:
                    continue
                reasoning = (f"Detected a recurring charge: '{c['name']}' "
                             f"~{c['amount']:,.0f}/{c.get('billing_cycle', 'monthly')}, "
                             f"seen {c.get('occurrences', '?')}x, last {c.get('last_date')}, "
                             f"confidence {c.get('confidence', 0):.0%}. Tracking it "
                             "enables renewal alerts and price-hike detection.")
                _emit_insight(events, ctx, "subscription",
                              f"New subscription detected: {c['name']}", reasoning,
                              {"name": c["name"], "amount": c["amount"],
                               "source_event_id": ev.get("id")})
                # propose through the registry → R3 gate parks it for approval
                ctx._extras["agent_name"] = "subscription_agent"
                ctx._extras["agent_reasoning"] = reasoning
                tools.invoke(ctx, "add_subscription",
                             {"name": c["name"], "monthly_cost": c["amount"],
                              "renewal_date": c.get("next_due")},
                             actor="agent")
        except Exception as exc:
            _report_error(events, "subscription", exc)

    for etype in _IMPORT_EVENTS:
        events.subscribe(etype, on_import)


def _screening_agent(events, ctx):
    """Values screening (R7A-1): after imports or manual adds, screen
    not-yet-checked transactions against the user's enabled ValuesProfiles.
    Flags carry reasoning and land in screening_flags (audit export);
    remediation (a review task) is proposed through the approval queue."""
    def on_new_transactions(ev):
        try:
            from ..values import (list_profiles, mark_screened, persist_flags,
                                  screen_transactions, unscreened_transactions)
            fe = ctx.open_finance()
            try:
                profiles = list_profiles(fe, enabled_only=True)
                if not profiles:
                    return
                txns = unscreened_transactions(fe, ctx.collab.conn)
                if not txns:
                    return
                flags = screen_transactions(fe, txns, profiles,
                                            llm=None)   # rules first; llm rules opt-in
            finally:
                fe.close()
            new = persist_flags(ctx.collab.conn, flags)
            mark_screened(ctx.collab.conn, [t["id"] for t in txns])
            if not new:
                return
            ns = ctx.notify_store()
            for f in flags[:5]:
                _emit_insight(events, ctx, "screening",
                              f"Values flag: {f['profile_name']}", f["reasoning"],
                              {"transaction_id": f["transaction_id"],
                               "severity": f["severity"],
                               "source_event_id": ev.get("id")})
            ref = f"screening_{ev.get('id')}"
            if not ns.exists_today("values_flag", ref):
                ns.create(type="values_flag",
                          title=f"{new} transaction(s) flagged by your values profiles",
                          body="; ".join(f["reasoning"] for f in flags[:3]),
                          priority="high" if any(f["severity"] == "high"
                                                 for f in flags) else "normal",
                          related_entity={"id": ref, "entity_type": "screening"})
            # remediation via the queue: a concrete review task, never a
            # silent data change
            from .. import tools
            from ..autonomous import GoalEngine
            goals = GoalEngine(ctx.collab)
            row = ctx.collab.conn.execute(
                "SELECT id FROM goals WHERE domain='finance'"
                " AND title='Values Review' AND status='active' LIMIT 1").fetchone()
            goal_id = row["id"] if row else goals.create_goal(
                "Values Review", domain="finance")
            worst = max(flags, key=lambda f: f["severity"] == "high")
            ctx._extras["agent_name"] = "screening_agent"
            ctx._extras["agent_reasoning"] = worst["reasoning"]
            ctx._extras["agent_dedup_key"] = f"values_task_{worst['transaction_id']}"
            tools.invoke(ctx, "add_goal_task",
                         {"goal_id": goal_id,
                          "title": f"Review flagged transaction: {worst['reasoning'][:120]}"},
                         actor="agent")

            # Interest purification (tathir): when a flag is INCOMING pure
            # interest (positive amount + interest-rule match), propose
            # donating exactly that amount to charity — the standard remedy
            # for money one may not keep. Proposal only: parks in the
            # Approval Inbox, deduped per source transaction.
            by_id = {t.get("id"): t for t in txns}
            for f in flags:
                txn = by_id.get(f["transaction_id"]) or {}
                amt = float(txn.get("amount") or 0)
                if amt <= 0 or "interest" not in (f.get("reasoning") or "").lower():
                    continue
                reasoning = (
                    f"Incoming pure interest of {amt:,.2f} from "
                    f"'{(txn.get('merchant') or '')[:60]}' on {txn.get('date')} "
                    f"was flagged by the '{f['profile_name']}' profile. "
                    "The standard remedy is purification: donate the exact "
                    "interest amount to charity. Approving records the "
                    "donation you make yourself — Amy never moves money.")
                ctx._extras["agent_name"] = "purification_agent"
                ctx._extras["agent_reasoning"] = reasoning
                ctx._extras["agent_dedup_key"] = f"purify_{f['transaction_id']}"
                tools.invoke(ctx, "add_transaction", {
                    "amount": -abs(amt),
                    "category": "Purification — interest donation",
                    "merchant": "Charity (interest purification)",
                    "notes": f"purifies txn {f['transaction_id']} ({txn.get('date')})",
                }, actor="agent")
        except Exception as exc:
            _report_error(events, "screening", exc)

    for etype in _TRANSACTION_EVENTS:
        events.subscribe(etype, on_new_transactions)


def _compliance_agent(events, ctx):
    def on_ledger_posted(ev):
        try:
            p = ev.get("payload") or {}
            entity_id = p.get("entity_id") or p.get("business_entity_id")
            if not entity_id:
                return
            fe = ctx.open_finance()
            try:
                entity = fe.get_business_entity(entity_id)
                if entity is None:
                    return
                if entity.get("tracking_closeness") != "close":
                    # loose tracking: the user asked for a lighter touch —
                    # do not run anything automatically (same gate the
                    # Auditor honors)
                    return
                pending = fe.ledger_entries_without_suggestions(entity_id)
                if not pending:
                    return
                from ..finance.business.compliance import generate_suggestions
                suggestions = generate_suggestions(fe, entity, _get_llm(ctx))
            finally:
                fe.close()
            reasoning = (f"Ledger entry posted for '{entity.get('name')}' "
                         f"(event {ev.get('id')}); entity is tracked 'close' and had "
                         f"{len(pending)} entr(y/ies) without compliance review — "
                         f"generated {len(suggestions)} advisory suggestion(s). "
                         "Suggestions are estimates, not professional tax advice.")
            _emit_insight(events, ctx, "compliance",
                          f"Compliance review: {entity.get('name')}", reasoning,
                          {"entity_id": entity_id,
                           "suggestions": len(suggestions),
                           "source_event_id": ev.get("id")})
        except Exception as exc:
            _report_error(events, "compliance", exc)

    events.subscribe("finance.ledger_entry_posted", on_ledger_posted)


_LEARNING_STALE_MIN_ITEMS = 10   # at least one real refresh cycle happened
_LEARNING_STALE_MIN_DAYS = 3     # give a fresh focus time before nudging


def _learning_agent(events, ctx):
    """Learning feed reactions (multi-focus, goal-linked): on a refresh,
    propose a goal for a topic that's organically trending and not yet
    linked to one; nudge (advisory only, never a write) a goal-linked
    focus that's accumulated items with zero saves/completions. On item
    completion, journal an insight — the activity-log write that feeds the
    trend engine already happens in the learning-feed router (save/
    progress endpoints), not here."""
    def on_feed_refreshed(ev):
        try:
            p = ev.get("payload") or {}
            topic = (p.get("focus") or "").strip()
            focus_id = p.get("focus_id")
            if not topic or not focus_id:
                return
            row = ctx.collab.conn.execute(
                "SELECT goal_id FROM learning_focuses WHERE id=?",
                (focus_id,)).fetchone()
            if row is None:
                return
            goal_id = row["goal_id"]

            if not goal_id:
                from ..collab.learning import LearningAgent
                trend = LearningAgent(ctx.collab, None).trends().get(topic)
                if not trend or trend["trend"] != "increasing":
                    return
                from .. import tools
                reasoning = (f"'{topic}' is trending up in your learning feed "
                             f"({trend['recent']} recent vs {trend['prior']} prior "
                             "engagement) and isn't linked to a goal yet — "
                             "proposing one so it's tracked deliberately.")
                _emit_insight(events, ctx, "learning",
                              f"'{topic}' is trending — suggest a goal", reasoning,
                              {"topic": topic, "focus_id": focus_id})
                ctx._extras["agent_name"] = "learning_agent"
                ctx._extras["agent_reasoning"] = reasoning
                ctx._extras["agent_dedup_key"] = f"learning_goal_{topic}"
                tools.invoke(ctx, "create_goal",
                             {"title": f"Deep-dive: {topic}", "domain": "learning"},
                             actor="agent")
                return

            # goal-linked focus: nudge (never propose a write) if it's stale
            stats = ctx.collab.conn.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(saved),0) AS saved_ct,"
                " MIN(fetched_at) AS first_fetch FROM learning_feed_items"
                " WHERE uid=? AND focus_id=?", (ctx.user_id, focus_id)).fetchone()
            if not stats or stats["total"] < _LEARNING_STALE_MIN_ITEMS or stats["saved_ct"]:
                return
            first_fetch = stats["first_fetch"]
            if not first_fetch:
                return
            age_days = (_dt.datetime.now(_dt.timezone.utc)
                       - _dt.datetime.fromisoformat(first_fetch)).days
            if age_days < _LEARNING_STALE_MIN_DAYS:
                return
            reasoning = (f"'{topic}' is linked to a goal but {stats['total']} curated "
                         f"items over {age_days} days have zero saves or completions — "
                         "flagging so it doesn't quietly go stale.")
            _emit_insight(events, ctx, "learning",
                          f"'{topic}' goal focus has no engagement yet", reasoning,
                          {"topic": topic, "focus_id": focus_id, "goal_id": goal_id})
            ns = ctx.notify_store()
            ref = f"learning_stale_{focus_id}"
            if not ns.exists_today("learning_stale_focus", ref):
                ns.create(type="learning_stale_focus",
                          title=f"No progress yet on '{topic}'",
                          body=reasoning, priority="normal",
                          related_entity={"id": ref, "entity_type": "learning_focus",
                                          "focus_id": focus_id, "goal_id": goal_id})
        except Exception as exc:
            _report_error(events, "learning", exc)

    def on_item_completed(ev):
        try:
            p = ev.get("payload") or {}
            title = p.get("title") or "an item"
            topic = p.get("focus") or ""
            reasoning = (f"Completed '{title}'"
                         + (f" (focus: {topic})" if topic else "") +
                         " — logged to the learning activity trail.")
            _emit_insight(events, ctx, "learning",
                          f"Completed: {title}", reasoning,
                          {"title": title, "focus": topic,
                           "focus_id": p.get("focus_id"),
                           "source_event_id": ev.get("id")})
        except Exception as exc:
            _report_error(events, "learning", exc)

    events.subscribe("learning.feed_refreshed", on_feed_refreshed)
    events.subscribe("learning.item_completed", on_item_completed)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_ERRAND_STOP = {"the", "and", "for", "from", "with", "near"}

# stemmed place-kind token → budget-category wording it usually hits
# (keys are post-_errand_stem forms: grocery→grocer, pharmacy→pharmac)
_KIND_BUDGET_ALIASES = {
    "grocer": "food groceries",
    "supermarket": "food groceries",
    "restaurant": "food dining",
    "cafe": "food dining",
    "mall": "shopping",
    "bazaar": "shopping",
    "pharmac": "health medical",
    "fuel": "transport",
    "petrol": "transport",
}


def _errand_stem(w: str) -> str:
    """grocery/groceries → grocer: strip plural-ish suffixes so a place kind
    matches the natural plural in a task title (substring checks don't —
    'grocery' is not a substring of 'groceries')."""
    for suf in ("ies", "es", "s", "y"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: len(w) - len(suf)]
    return w


def _errand_tokens(text: str) -> set[str]:
    import re
    return {_errand_stem(t) for t in re.split(r"[^a-z0-9]+", text.lower())
            if len(t) >= 3 and t not in _ERRAND_STOP}


def _errand_agent(events, ctx):
    """Errand geofencing (CONTEXT_PLAN C1): when the location sensor reports
    entering a saved place, remind about open tasks that belong there.

    Match order: explicit tasks.place_tag (equals the place's kind or name),
    then keyword fallback (a stemmed token of the place's kind/name appears in
    the task title). Reminders dedup per task+place per 24h. Coordinates never
    reach this agent — the event payload carries only place id/name/kind."""
    def on_place_entered(ev):
        try:
            p = ev.get("payload") or {}
            place_id = p.get("place_id") or ""
            name = (p.get("name") or "").strip()
            kind = (p.get("kind") or "").strip().lower()
            if not place_id or not (name or kind):
                return
            tokens = _errand_tokens(kind + " " + name)
            rows = ctx.collab.conn.execute(
                "SELECT id, title, COALESCE(place_tag,'') AS place_tag"
                " FROM tasks WHERE done=0").fetchall()
            ns = ctx.notify_store()
            for t in rows:
                title = (t["title"] or "").strip()
                tag = (t["place_tag"] or "").strip().lower()
                tagged = tag and tag in (kind, name.lower())
                keyword = bool(tokens & _errand_tokens(title))
                if not (tagged or keyword):
                    continue
                reasoning = (f"You just arrived at '{name}'"
                             f"{f' ({kind})' if kind else ''} and the open task "
                             f"'{title}' matches it "
                             f"({'place tag' if tagged else 'title keyword'}).")
                _emit_insight(events, ctx, "errand",
                              f"Near {name}: {title}", reasoning,
                              {"task_id": t["id"], "place_id": place_id,
                               "source_event_id": ev.get("id")})
                ref = f"errand_{t['id']}_{place_id}"
                if not ns.exists_today("errand_reminder", ref):
                    ns.create(type="errand_reminder",
                              title=f"You're near {name}",
                              body=f"Open task: {title} — good time to knock it out.",
                              priority="normal",
                              related_entity={"id": ref, "entity_type": "task",
                                              "task_id": t["id"],
                                              "place_id": place_id})
        except Exception as exc:
            _report_error(events, "errand", exc)

    events.subscribe("context.place_entered", on_place_entered)


# ---------------------------------------------------------------------------
# CONNECTOR COMPLETION Part 2 — pr_to_task + meeting_prep
# ---------------------------------------------------------------------------

_CHANGES_REQUESTED_STATES = {"changes_requested", "changesrequested", "request_changes"}


def _pr_to_task_agent(events, ctx):
    """A PR that needs the user's review — or one that just went to
    changes-requested — becomes a Plane task proposal. plane_create_task is
    an EXTERNAL tool (amy/tools/connector_tools.py), so this always parks at
    tier 2 (amy/automation/executors.py's _tier_for) regardless of
    AMY_AGENT_WRITE_TIER. Deduped per PR (dedup_key pr_task_{repo}_{number})
    so re-polling the same PR — or a status-changed event landing right
    after a review-requested one for the same PR — never proposes twice."""
    def on_pr_event(ev):
        try:
            p = ev.get("payload") or {}
            repo, number = p.get("repo"), p.get("number")
            title, url = p.get("title") or "", p.get("url") or ""
            if not repo or number is None:
                return
            if ev.get("type") == "github.pr_status_changed":
                state = (p.get("state") or "").lower().replace(" ", "_")
                if state not in _CHANGES_REQUESTED_STATES:
                    return
                reason = "went to changes-requested"
            else:
                reason = "needs your review"

            summary = f"PR #{number} in {repo}: {title}".strip()
            llm = _get_llm(ctx)
            if llm is not None:
                try:
                    text, _ = llm.generate(
                        "Summarize in ONE short sentence why this pull "
                        "request needs the user's attention right now.",
                        f"Repo: {repo}\nPR #{number}: {title}\nURL: {url}\n"
                        f"Reason: {reason}")
                    if text and text.strip():
                        summary = text.strip()[:300]
                except Exception:
                    pass   # LLM summary is a nice-to-have; the reasoning below still works

            reasoning = f"PR #{number} in {repo} ({title}) {reason}. {summary}"
            _emit_insight(events, ctx, "pr_to_task", f"PR needs attention: {title}",
                         reasoning, {"repo": repo, "number": number, "url": url,
                                     "source_event_id": ev.get("id")})

            from .. import tools
            ctx._extras["agent_name"] = "pr_to_task_agent"
            ctx._extras["agent_reasoning"] = reasoning
            ctx._extras["agent_dedup_key"] = f"pr_task_{repo}_{number}"
            tools.invoke(ctx, "plane_create_task",
                         {"title": f"Review PR #{number}: {title}"[:200],
                          "description": f"{url}\n\n{reasoning}"[:2000]},
                         actor="agent")
        except Exception as exc:
            _report_error(events, "pr_to_task", exc)

    events.subscribe("github.pr_review_requested", on_pr_event)
    events.subscribe("github.pr_status_changed", on_pr_event)


_MEETING_PREP_DEFAULT_WINDOW_MIN = 60


def _meeting_prep_window_minutes() -> int:
    try:
        return int(config._env("AMY_MEETING_PREP_WINDOW_MIN",
                               str(_MEETING_PREP_DEFAULT_WINDOW_MIN)))
    except ValueError:
        return _MEETING_PREP_DEFAULT_WINDOW_MIN


def _meeting_prep_agent(events, ctx):
    """No-op subscription: unlike every other agent here, meeting_prep has
    no natural triggering EVENT — "a meeting is starting soon" only exists
    by polling the calendar, so there's nothing to .subscribe() to. This
    function exists only so meeting_prep is visible/consistent in
    register_reactive_agents' registered-agents list and honors its kill
    switch the same way the others do; the real work is
    meeting_prep_check(), called directly by the meeting_prep_scan job
    (amy/automation/jobs.py) every 15 minutes."""
    return


def meeting_prep_check(events, ctx) -> int:
    """Read-only, tier 0 — NEVER proposes a write (unlike pr_to_task).
    For each Google Calendar meeting starting within the prep window
    (AMY_MEETING_PREP_WINDOW_MIN, default 60 min), keyword-matches its
    title/attendees against Plane tasks and GitHub PRs, writes ONE
    idempotent vault note per meeting id (MemoryWriter dedups on eid — safe
    to call every 15 minutes without re-writing), and emits agent.insight.
    Returns the number of meetings prepped this call. Never raises —
    connector failures degrade to an empty related-items list, not a
    skipped meeting."""
    if not config.agent_enabled("meeting_prep"):
        return 0
    from .. import tools
    from ..connectors.mcp_call import extract_list

    window_min = _meeting_prep_window_minutes()
    try:
        meetings = tools.invoke(ctx, "meet_upcoming_meetings",
                                {"hours": max(1, window_min // 60 + 1)}, actor="agent")
    except Exception as exc:
        _report_error(events, "meeting_prep", exc)
        return 0

    now = _dt.datetime.now(_dt.timezone.utc)
    prepped = 0
    for m in (meetings or {}).get("meetings", []):
        try:
            start_raw = m.get("start") or ""
            try:
                start = _dt.datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                if start.tzinfo is None:
                    start = start.replace(tzinfo=_dt.timezone.utc)
            except Exception:
                continue   # all-day events (date, not dateTime) have no meaningful "minutes away"
            minutes_away = (start - now).total_seconds() / 60
            if not (0 <= minutes_away <= window_min):
                continue
            meeting_id = m.get("id") or ""
            title = m.get("title") or "(no title)"
            if not meeting_id:
                continue

            keywords = _errand_tokens(title)
            for att in (m.get("attendees") or []):
                keywords |= _errand_tokens(att.split("@")[0])

            related_tasks: list[str] = []
            try:
                plane = tools.invoke(ctx, "plane_list_tasks", {}, actor="agent")
                for t in extract_list(plane):
                    name = str(t.get("name") or t.get("title") or "")
                    if name and (_errand_tokens(name) & keywords):
                        related_tasks.append(name)
            except Exception:
                pass   # related-item lookups are best-effort; the note still gets written

            related_prs: list[str] = []
            try:
                gh = tools.invoke(ctx, "github_list_prs", {}, actor="agent")
                for pr in extract_list(gh):
                    ttl = str(pr.get("title") or "")
                    if ttl and (_errand_tokens(ttl) & keywords):
                        related_prs.append(ttl)
            except Exception:
                pass

            lines = [f"Meeting: **{title}** at {start_raw}", ""]
            if m.get("meet_link"):
                lines.append(f"[Join]({m['meet_link']})")
            if related_tasks:
                lines.append("\nRelated Plane tasks:")
                lines += [f"- {t}" for t in related_tasks[:5]]
            if related_prs:
                lines.append("\nRelated GitHub activity:")
                lines += [f"- {t}" for t in related_prs[:5]]
            if not related_tasks and not related_prs:
                lines.append("\nNo related Plane tasks or GitHub activity found by keyword match.")

            reasoning = (f"Meeting '{title}' starts in {int(minutes_away)} minute(s) — "
                        f"prepped {len(related_tasks)} related task(s) and "
                        f"{len(related_prs)} related PR/issue(s) by keyword match.")
            try:
                from ..memory.writer import MemoryWriter
                from ..saas import tenancy
                vault = tenancy.resolve_vault_dir(ctx.user_id)
                vault.mkdir(parents=True, exist_ok=True)
                MemoryWriter(vault).write_atomic(
                    "meeting prep", title[:50], "\n".join(lines),
                    eid=f"meetingprep-{meeting_id}", tags=["meeting", "prep"])
            except Exception:
                pass   # journaling is best-effort; the insight event is still the record
            _emit_insight(events, ctx, "meeting_prep", f"Prepped: {title}", reasoning,
                         {"meeting_id": meeting_id, "title": title})
            prepped += 1
        except Exception as exc:
            _report_error(events, "meeting_prep", exc)
    return prepped


def register_reactive_agents(events, ctx) -> list[str]:
    """Wire enabled reactive agents onto this EventStore instance.

    Idempotent per EventStore instance (Part 0 / quirk 20 fix, RISK B): each
    agent is subscribed at most once per instance, tracked via
    events._registered_agent_keys. Calling this twice on the same store (or
    calling amy.events.factory.get_events() twice for it) is safe — the
    second call subscribes nothing new, so a single emit still runs each
    agent's handler exactly once. Falls back to a fresh local set if the
    instance predates this attribute (shouldn't happen; EventStore.__init__
    always sets it) so this never raises on an unusual store implementation.

    Returns the names of every agent ACTIVE on this instance (cumulative,
    not just newly-registered-this-call) for tests/observability.
    """
    seen = getattr(events, "_registered_agent_keys", None)
    if seen is None:
        seen = set()
        try:
            events._registered_agent_keys = seen
        except Exception:
            pass

    def _once(name: str, register_fn) -> None:
        if name in seen:
            return
        register_fn(events, ctx)
        seen.add(name)

    if config.agent_enabled("budget"):
        _once("budget", _budget_agent)
    if config.agent_enabled("subscription"):
        _once("subscription", _subscription_agent)
    if config.agent_enabled("compliance"):
        _once("compliance", _compliance_agent)
    if config.agent_enabled("screening"):
        _once("screening", _screening_agent)
    if config.agent_enabled("errand"):
        _once("errand", _errand_agent)
    if config.agent_enabled("learning"):
        _once("learning", _learning_agent)
    if config.agent_enabled("pr_task"):
        _once("pr_task", _pr_to_task_agent)
    if config.agent_enabled("meeting_prep"):
        _once("meeting_prep", _meeting_prep_agent)
    return sorted(seen)
