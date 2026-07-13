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
import json
import logging
import re

from .. import config

_log = logging.getLogger("amy.agents.reactive")

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
    linked to one — UNLESS it looks role-shaped and there's already an
    active career goal, in which case the focus is cross-linked to that
    goal instead of spawning a parallel one (the career template's own
    _skill_gaps() step already tracks the same kind of topic there).
    Nudges (advisory only, never a write) a goal-linked focus that's
    accumulated items with zero saves/completions, and suggests another
    organically-trending, not-yet-tracked topic as an alternative if one
    exists. On item completion for a goal-linked focus, adds a completed
    task to that goal (GoalEngine.progress() counts done tasks, so this is
    what actually moves the needle instead of only journaling an insight)."""
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

                # Cross-link into an existing career plan instead of a
                # parallel "Deep-dive" goal when the topic looks role-shaped
                # and a career goal is already active — same trend signal
                # _career_goal_agent uses to decide whether to propose a
                # brand-new career goal in the first place.
                if any(r in topic.lower() for r in _CAREER_ROLE_WORDS):
                    career_gid = _active_career_goal(ctx)
                    if career_gid:
                        from ..learning_feed.sensor import set_focus_goal
                        set_focus_goal(ctx.collab.conn, ctx.user_id, focus_id, career_gid)
                        reasoning = (f"'{topic}' is trending up and looks role-related; "
                                     f"you already have an active career goal, so linking "
                                     "this focus there instead of starting a separate "
                                     "learning goal that would just duplicate it.")
                        _emit_insight(events, ctx, "learning",
                                      f"Linked '{topic}' to your career goal", reasoning,
                                      {"topic": topic, "focus_id": focus_id,
                                       "goal_id": career_gid})
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

            # goal-linked focus + course items (COURSES SOURCE): a highly
            # relevant free course for a goal-linked gap proposes ONE tier-2
            # "take this course" task on that goal — a WRITE, so it goes
            # through the approval inbox; the feed items themselves stay
            # advisory/ungated. Dedup key = per course URL, so a course is
            # proposed at most once ever, never auto-added.
            import hashlib as _hl

            courses = ctx.collab.conn.execute(
                "SELECT title, url, relevance FROM learning_feed_items"
                " WHERE uid=? AND focus_id=? AND source='courses'"
                " AND COALESCE(relevance,0) >= 8"
                " ORDER BY relevance DESC LIMIT 3",
                (ctx.user_id, focus_id)).fetchall()
            if courses:
                from .. import tools
                for c in courses:
                    key = f"course_{focus_id}_{_hl.sha1((c['url'] or '').encode()).hexdigest()[:8]}"
                    reasoning = (f"'{c['title']}' scored {c['relevance']}/10 for "
                                 f"'{topic}', which is linked to your goal — "
                                 f"proposing it as a goal task. {c['url']}")
                    ctx._extras["agent_name"] = "learning_agent"
                    ctx._extras["agent_reasoning"] = reasoning
                    ctx._extras["agent_dedup_key"] = key
                    try:
                        tools.invoke(ctx, "add_goal_task",
                                     {"goal_id": goal_id,
                                      "title": f"Take course: {c['title']} — {c['url']}"},
                                     actor="agent")
                    except Exception:
                        pass   # one bad course row must not block the rest

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
            alt = _suggest_alternative_topic(ctx, exclude_topic=topic)
            if alt:
                reasoning += (f" '{alt}' is trending up in your activity and isn't "
                               "tracked as a focus yet, if you'd rather switch.")
            _emit_insight(events, ctx, "learning",
                          f"'{topic}' goal focus has no engagement yet", reasoning,
                          {"topic": topic, "focus_id": focus_id, "goal_id": goal_id,
                           "suggested_alternative": alt})
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
            focus_id = p.get("focus_id")
            reasoning = (f"Completed '{title}'"
                         + (f" (focus: {topic})" if topic else "") +
                         " — logged to the learning activity trail.")
            _emit_insight(events, ctx, "learning",
                          f"Completed: {title}", reasoning,
                          {"title": title, "focus": topic,
                           "focus_id": focus_id,
                           "source_event_id": ev.get("id")})

            # Goal-based learning: a completion on a goal-linked focus adds a
            # DONE task to that goal, so GoalEngine.progress() (done tasks +
            # milestones / total) actually advances — previously this only
            # ever wrote an insight, so linking a focus to a goal had no
            # effect on the goal's own progress number.
            if not focus_id:
                return
            row = ctx.collab.conn.execute(
                "SELECT goal_id FROM learning_focuses WHERE id=?",
                (focus_id,)).fetchone()
            goal_id = row["goal_id"] if row else None
            if not goal_id:
                return
            task_title = f"Learned: {title}"[:200]
            dup = ctx.collab.conn.execute(
                "SELECT 1 FROM tasks WHERE goal_id=? AND title=?",
                (goal_id, task_title)).fetchone()
            if dup:
                return
            from ..autonomous import GoalEngine
            engine = GoalEngine(ctx.collab, events=events)
            tid = engine.add_task(goal_id, task_title)
            engine.complete_task(tid, done=True)
        except Exception as exc:
            _report_error(events, "learning", exc)

    events.subscribe("learning.feed_refreshed", on_feed_refreshed)
    events.subscribe("learning.item_completed", on_item_completed)


def _suggest_alternative_topic(ctx, exclude_topic: str) -> str | None:
    """Best organically-increasing topic (from the activity-log trend
    engine) that isn't already an active learning focus for this user —
    used to give a stale-focus nudge a concrete alternative instead of
    just flagging the problem. Advisory data only, never a write."""
    from ..collab.learning import LearningAgent
    trends = LearningAgent(ctx.collab, None).trends()
    tracked = {r["topic"].strip().lower() for r in ctx.collab.conn.execute(
        "SELECT topic FROM learning_focuses WHERE uid=? AND active=1",
        (ctx.user_id,)).fetchall()}
    candidates = [(t, data) for t, data in trends.items()
                  if data["trend"] == "increasing"
                  and t.strip().lower() not in tracked
                  and t.strip().lower() != exclude_topic.strip().lower()]
    if not candidates:
        return None
    candidates.sort(key=lambda kv: -kv[1]["recent"])
    return candidates[0][0]


# ---------------------------------------------------------------------------
# career_goal (CAREER AUTOPILOT Part 2)
# ---------------------------------------------------------------------------

# Duplicated (not imported) from amy/automation/orchestrator.py's role-word
# list on purpose — same stance as executors.py/connector_tools.py's
# duplicated Plane candidate tuples: importing orchestrator here would
# couple the reactive-agents module to the orchestrator module for one
# small constant, and reactive.py is imported from amy.events.factory,
# which orchestrator.py's own callers sit downstream of.
_CAREER_ROLE_WORDS = ("engineer", "developer", "dev", "designer", "scientist",
                     "analyst", "manager", "architect", "specialist",
                     "consultant", "researcher", "programmer")

_CAREER_STALL_DEFAULT_DAYS = 5
_CAREER_STALL_WINDOW_DAYS = 3   # nudge once in this window, then go quiet — same idiom as relationship_nudges


def _active_career_goal(ctx) -> str | None:
    row = ctx.collab.conn.execute(
        "SELECT id FROM goals WHERE domain='career' AND status='active'"
        " ORDER BY created_at DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def _career_goal_agent(events, ctx):
    """(a) No active career goal yet, but a learning focus is trending
    toward a role-shaped topic ('genai engineer', ...) — propose a career
    goal (tier-2, dedup 'career_goal_suggest') so it gets a real plan
    (portfolio, job search, milestones) instead of staying just a reading
    topic. Reuses the exact trend signal _learning_agent already computes
    via LearningAgent.trends() — no new instrumentation.

    (b) Stall nudge (an active career goal with no recent career.* activity)
    has no natural push event ('N days of silence' isn't a thing that
    happens) — see career_goal_stall_check() below, driven by the
    career_goal_stall_check job, same structural choice as meeting_prep's
    job-driven meeting_prep_check()."""
    def on_feed_refreshed(ev):
        try:
            if _active_career_goal(ctx):
                return
            p = ev.get("payload") or {}
            topic = (p.get("focus") or "").strip()
            focus_id = p.get("focus_id")
            if not topic or not focus_id:
                return
            if not any(r in topic.lower() for r in _CAREER_ROLE_WORDS):
                return
            from ..collab.learning import LearningAgent
            trend = LearningAgent(ctx.collab, None).trends().get(topic)
            if not trend or trend["trend"] != "increasing":
                return
            from .. import tools
            reasoning = (f"'{topic}' is trending up in your learning feed "
                         f"({trend['recent']} recent vs {trend['prior']} prior "
                         "engagement) and looks role-shaped — proposing a "
                         "career goal so it gets a real plan (portfolio "
                         "review, job search, weekly milestones) instead of "
                         "staying just a reading topic.")
            _emit_insight(events, ctx, "career_goal",
                          f"'{topic}' looks like a career direction", reasoning,
                          {"topic": topic, "focus_id": focus_id})
            ctx._extras["agent_name"] = "career_goal_agent"
            ctx._extras["agent_reasoning"] = reasoning
            ctx._extras["agent_dedup_key"] = "career_goal_suggest"
            tools.invoke(ctx, "create_goal",
                         {"title": f"Become a {topic}", "domain": "career"},
                         actor="agent")
        except Exception as exc:
            _report_error(events, "career_goal", exc)

    events.subscribe("learning.feed_refreshed", on_feed_refreshed)


def career_goal_stall_check(events, ctx) -> dict:
    """Job-driven (career_goal_stall_check job — daily). Advisory nudge
    only, fired once in a bounded window then goes quiet, same non-nag
    idiom as amy/patterns.py's relationship_nudges.

    Known simplification: checks for ANY career.* event since the goal was
    created (system-wide), not one tagged with this specific goal_id — most
    career.* payloads (job_discovered, application_*) aren't per-goal
    today. Acceptable because exactly one active career-domain goal is the
    expected steady state; if multi-goal career tracking is ever added,
    tag goal_id on every career.* emit and tighten this query."""
    from .. import config
    stall_days = int(config._env("AMY_CAREER_STALL_DAYS", str(_CAREER_STALL_DEFAULT_DAYS)))
    rows = ctx.collab.conn.execute(
        "SELECT id, title, created_at FROM goals WHERE domain='career' AND status='active'"
    ).fetchall()
    ns = ctx.notify_store()
    nudged = 0
    for g in rows:
        last = ctx.collab.conn.execute(
            "SELECT MAX(ts) m FROM events WHERE type LIKE 'career.%' AND ts>?",
            (g["created_at"],)).fetchone()
        anchor = (last["m"] if last else None) or g["created_at"]
        if not anchor:
            continue
        try:
            age_days = (_dt.datetime.now(_dt.timezone.utc)
                       - _dt.datetime.fromisoformat(anchor)).days
        except ValueError:
            continue
        days_over = age_days - stall_days
        if not (0 <= days_over <= _CAREER_STALL_WINDOW_DAYS):
            continue
        ref = f"career_stall_{g['id']}"
        if ns.exists_today("career_stall", ref):
            continue
        reasoning = (f"Career goal '{g['title']}' has had no application/"
                     f"portfolio/job-discovery activity in {age_days} day(s) "
                     f"(stall threshold: {stall_days}d) — flagging once in "
                     "case it's just been busy, not because it's overdue.")
        ns.create(type="career_stall", title=f"No recent progress: {g['title']}",
                  body=reasoning, priority="normal",
                  related_entity={"id": ref, "entity_type": "goal", "goal_id": g["id"]})
        _emit_insight(events, ctx, "career_goal",
                      f"No progress on '{g['title']}' in {age_days}d", reasoning,
                      {"goal_id": g["id"]})
        nudged += 1
    return {"checked": len(rows), "nudged": nudged}


# ---------------------------------------------------------------------------
# portfolio (CAREER AUTOPILOT Part 3) — GitHub <-> career
# ---------------------------------------------------------------------------

_PORTFOLIO_MIN_SHOWCASE_OVERLAP = 2
_PORTFOLIO_MAX_GAPS = 5

_PORTFOLIO_SYSTEM = (
    "You help tailor a developer's GitHub portfolio for a target job role. "
    "For each SHOWCASE repo, write one sentence on why it belongs on a "
    "resume for this role plus 2-3 punchy bullet points (technologies + "
    "outcomes) — infer outcomes conservatively from the repo name/"
    "description/topics given, never invent metrics that aren't implied. "
    "For each GAP keyword (a skill the role wants that no repo evidences), "
    "suggest ONE concrete buildable project. Respond with EXACTLY ONE JSON "
    'object: {"showcase": [{"repo": "<name>", "why": "...", '
    '"bullets": ["...", "..."]}], "gap_projects": [{"keyword": "...", '
    '"project_idea": "..."}]}'
)


def _portfolio_agent(events, ctx):
    """No-op subscription, same reasoning as _meeting_prep_agent: portfolio
    analysis triggers are 'on demand' (a career plan step, a manual button)
    and 'monthly' (portfolio_review job) — none of those are a push EVENT
    to .subscribe() to. Exists only so 'portfolio' is visible/consistent in
    register_reactive_agents' list and honors its kill switch. Real logic:
    portfolio_analyze(), called directly by the orchestrator's career
    template, the portfolio_review job, and (Part 6) a manual API route."""
    return


def _repo_text(repo: dict) -> str:
    parts = [str(repo.get("name") or repo.get("full_name") or ""),
             str(repo.get("description") or ""),
             str(repo.get("language") or "")]
    parts += [str(t) for t in (repo.get("topics") or [])]
    return " ".join(parts)


def _classify_repos(repos: list[dict], role_keywords: set[str]) -> tuple[list, list, list]:
    """Deterministic, auditable classification (not LLM-decided — the
    factors are the keyword overlap + activity/polish signals shown below,
    matching the 'estimates, factors shown' requirement) into SHOWCASE /
    NEEDS WORK / NOT RELEVANT. Missing-signal detection is limited to what
    a repo-list/detail call actually returns (description, homepage,
    topics) — there is no MCP call for a repo's file tree, so 'tests' isn't
    claimed as detected, only suggested as a standard checklist item."""
    kw_lower = {k.lower() for k in role_keywords}
    showcase, needs_work, not_relevant = [], [], []
    for r in repos:
        if r.get("archived") or r.get("fork"):
            not_relevant.append(r)
            continue
        text_l = _repo_text(r).lower()
        matched = sorted(k for k in kw_lower if k in text_l)
        r["_matched_keywords"] = matched
        if not matched:
            not_relevant.append(r)
            continue
        missing = []
        if not (r.get("description") or "").strip():
            missing.append("README/description")
        if not (r.get("homepage") or "").strip():
            missing.append("demo/deployment link")
        if not (r.get("topics") or []):
            missing.append("topics for discoverability")
        r["_missing"] = missing
        if len(matched) >= _PORTFOLIO_MIN_SHOWCASE_OVERLAP and not missing:
            showcase.append(r)
        else:
            needs_work.append(r)
    return showcase, needs_work, not_relevant


def _portfolio_fallback_entry(repo: dict, target_role: str) -> dict:
    matched = repo.get("_matched_keywords") or []
    name = repo.get("name") or repo.get("full_name") or "repo"
    return {"repo": name,
           "why": f"Matches {len(matched)} keyword(s) for {target_role}: "
                  f"{', '.join(matched) or 'general relevance'}.",
           "bullets": [f"Built with {repo.get('language') or 'multiple technologies'}",
                       f"Topics: {', '.join(repo.get('topics') or []) or 'none listed'}"]}


def portfolio_analyze(events, ctx, target_role: str | None = None,
                      goal_id: str | None = None) -> dict:
    """Pull repos (GitHub MCP) -> classify against a target-role keyword
    profile built from REAL job postings (job_search, never LLM memory) ->
    SHOWCASE/NEEDS WORK/NOT RELEVANT + gap project suggestions -> vault note
    + career.portfolio_analyzed event. Repo/keyword analysis is
    sensitive=False (public repo metadata + role keywords, no resume text);
    the ONE narrative-writing LLM call stays in that same non-sensitive
    lane for the same reason. Gap projects worth building are proposed as
    ONE batched Plane approval (plane_batch_create_tasks, external -> tier
    2), same atomic-batch pattern Part 2 uses for milestones."""
    from .. import tools
    from ..connectors.mcp_call import extract_list

    if not target_role and goal_id:
        row = ctx.collab.conn.execute(
            "SELECT career_meta FROM goals WHERE id=?", (goal_id,)).fetchone()
        if row and row["career_meta"]:
            try:
                meta = json.loads(row["career_meta"])
                # Part 5F ladder: the portfolio builds toward the DESTINATION
                # role — you apply with what you have, you build toward where
                # you're going (scouting stays on meta["target_role"]).
                target_role = (meta.get("north_star_role")
                               or meta.get("target_role"))
            except Exception:
                pass
    if not target_role:
        profile = ctx.store.get_career_profile(ctx.user_id) or {}
        target_role = profile.get("target_role")
    if not target_role:
        return {"skipped": "no target_role on file (set a career profile or goal first)"}

    try:
        repo_out = tools.invoke(ctx, "portfolio_repo_list", {}, actor="agent")
        repos = repo_out.get("repos") or []
    except Exception as exc:
        return {"error": f"portfolio_repo_list failed: {str(exc)[:200]}"}
    if not repos:
        return {"skipped": "no repositories found on the connected GitHub account"}

    keywords: set[str] = set()
    try:
        job_out = tools.invoke(ctx, "job_search",
                               {"search_term": target_role, "results_wanted": 10},
                               actor="agent")
        postings = job_out.get("jobs") or []
        from ..automation.orchestrator import _extract_keywords
        keywords = set(_extract_keywords(postings, top_n=15))
    except Exception as exc:
        _report_error(events, "portfolio", exc)
    if not keywords:
        keywords = {target_role}

    showcase, needs_work, not_relevant = _classify_repos(repos, keywords)
    evidenced = {k for r in (showcase + needs_work + not_relevant)
                for k in (r.get("_matched_keywords") or [])}
    gaps = sorted({k.lower() for k in keywords} - evidenced)[:_PORTFOLIO_MAX_GAPS]

    entries = [_portfolio_fallback_entry(r, target_role) for r in showcase]
    gap_projects = [{"keyword": g, "project_idea": f"Build a small {g} project "
                     "to demonstrate this skill."} for g in gaps]

    llm = _get_llm(ctx)
    if llm is not None and (showcase or gaps):
        repo_lines = [f"- {r.get('name')}: {r.get('description') or '(no description)'} "
                      f"[{', '.join(r.get('_matched_keywords') or [])}]" for r in showcase]
        prompt = (f"Target role: {target_role}\nGap keywords: {', '.join(gaps) or 'none'}\n\n"
                 f"Showcase repos:\n" + "\n".join(repo_lines))
        try:
            text, provider = llm.generate(_PORTFOLIO_SYSTEM, prompt, sensitive=False)
            if provider != "template":
                m = re.search(r"\{.*\}", text, re.DOTALL)
                if m:
                    parsed = json.loads(m.group(0))
                    if isinstance(parsed.get("showcase"), list) and parsed["showcase"]:
                        entries = parsed["showcase"]
                    if isinstance(parsed.get("gap_projects"), list) and parsed["gap_projects"]:
                        gap_projects = parsed["gap_projects"]
        except Exception as exc:
            _log.warning("portfolio: LLM narrative pass failed, using fallback: %s", exc)

    # CAREER AUTOPILOT Phase D: persist the classification this SAME pass
    # just computed — previously reached only a vault note as formatted
    # text (see amy/career_portfolio.py's module docstring).
    try:
        from ..career_portfolio import persist_classification
        entries_by_repo = {str(e.get("repo") or ""): e for e in entries}
        persist_classification(ctx, target_role, showcase, needs_work, not_relevant,
                               entries_by_repo=entries_by_repo)
    except Exception as exc:
        _log.warning("portfolio: persisting classification failed: %s", exc)

    queued = 0
    if gap_projects:
        reasoning = (f"Portfolio gap projects for '{target_role}': {len(gap_projects)} "
                     "skill(s) no current repo evidences, batched into one approval.")
        ctx._extras["agent_name"] = "portfolio_agent"
        ctx._extras["agent_reasoning"] = reasoning
        ctx._extras["agent_dedup_key"] = f"portfolio_gaps_{ctx.user_id}_{target_role}"
        try:
            batch = tools.invoke(
                ctx, "plane_batch_create_tasks",
                {"tasks": [{"title": f"Portfolio gap: {g['keyword']}",
                           "description": g["project_idea"]} for g in gap_projects]},
                actor="agent")
            if isinstance(batch, dict) and batch.get("status") == "pending":
                queued = 1
        except Exception as exc:
            _log.warning("portfolio: gap-project batch proposal failed: %s", exc)

    today = _dt.date.today().isoformat()
    lines = [f"Target role: **{target_role}**", "",
            f"### Showcase ({len(showcase)})"]
    for e in entries:
        lines.append(f"- **{e.get('repo')}** — {e.get('why', '')}")
        for b in e.get("bullets") or []:
            lines.append(f"  - {b}")
    lines.append(f"\n### Needs work ({len(needs_work)})")
    for r in needs_work:
        missing = ", ".join(r.get("_missing") or []) or "polish"
        lines.append(f"- **{r.get('name')}** — relevant, missing: {missing}")
    lines.append(f"\n### Gaps ({len(gap_projects)})")
    for g in gap_projects:
        lines.append(f"- **{g['keyword']}**: {g['project_idea']}")
    note_body = "\n".join(lines)

    note_path = None
    try:
        from ..memory.writer import MemoryWriter
        from ..saas import tenancy
        vault = tenancy.resolve_vault_dir(ctx.user_id)
        vault.mkdir(parents=True, exist_ok=True)
        p = MemoryWriter(vault).write_atomic(
            "portfolio review", f"Portfolio Review - {today}", note_body,
            eid=f"portfolioreview-{ctx.user_id}-{today}", tags=["career", "portfolio"])
        note_path = str(p) if p else "already-written"
    except Exception as exc:
        _log.warning("portfolio: vault note failed: %s", exc)

    result = {"target_role": target_role, "showcase": entries,
             "needs_work": [{"repo": r.get("name"), "missing": r.get("_missing")}
                           for r in needs_work],
             "not_relevant_count": len(not_relevant), "gaps": gap_projects,
             "note": note_path, "queued_approvals": queued}

    try:
        from ..events.store import CAREER_PORTFOLIO_ANALYZED
        payload = {"agent": "portfolio_agent", "target_role": target_role,
                  "goal_id": goal_id, "showcase_count": len(showcase),
                  "needs_work_count": len(needs_work), "gap_count": len(gap_projects),
                  "reasoning": f"Analyzed {len(repos)} repo(s) against '{target_role}'."}
        eid = events.emit(CAREER_PORTFOLIO_ANALYZED, payload, source="portfolio_agent")
        _journal(ctx, {"id": eid, "type": CAREER_PORTFOLIO_ANALYZED, "payload": payload,
                       "ts": None, "source": "portfolio_agent"})
    except Exception:
        pass   # fire-and-forget: the result dict + vault note are the record

    # Part 5E master resume evolution: the analysis just produced real,
    # repo-evidenced bullets — propose folding them into the master resume
    # (tier 2, diff in the approval body) instead of letting it stay frozen.
    try:
        result["resume_proposal"] = _propose_resume_evolution(ctx, entries)
    except Exception as exc:
        _log.warning("portfolio: resume evolution proposal failed: %s", exc)

    return result


_RESUME_HIGHLIGHTS_HEADER = "## Project highlights"


def _propose_resume_evolution(ctx, showcase_entries: list[dict]) -> str | None:
    """Part 5E: propose a career_profile.resume_text update from portfolio
    bullets — ONLY repo-evidenced content (each bullet names a real repo the
    classifier just verified), never ATS keyword stuffing: missing ATS
    keywords are skills the resume doesn't evidence, and inserting them
    would put claims in the user's mouth. Tier 2 with a unified diff in the
    approval body; the resume_update executor applies it on approve. Deduped
    per month so a re-run doesn't re-propose the same evolution."""
    from difflib import unified_diff

    from ..automation.executors import submit_action

    profile = ctx.store.get_career_profile(ctx.user_id) or {}
    current = (profile.get("resume_text") or "").strip()
    if not current:
        return None   # no master resume on file — nothing to evolve
    bullets: list[str] = []
    for e in showcase_entries:
        repo = str(e.get("repo") or "").strip()
        for b in (e.get("bullets") or []):
            line = f"- {b}" if repo and repo in str(b) else f"- {repo}: {b}"
            if line.lower() not in current.lower():
                bullets.append(line)
    if not bullets:
        return None   # everything already reflected in the resume

    section = f"{_RESUME_HIGHLIGHTS_HEADER}\n" + "\n".join(bullets[:6])
    proposed = f"{current}\n\n{section}\n"
    diff = "\n".join(unified_diff(current.splitlines(), proposed.splitlines(),
                                  fromfile="resume (current)",
                                  tofile="resume (proposed)", lineterm=""))
    month = _dt.date.today().strftime("%Y-%m")
    out = submit_action(
        ctx, 2, "resume_update",
        title="Resume update: fold in portfolio highlights",
        body=("Portfolio analysis produced repo-evidenced resume bullets. "
              "Diff of the proposed change:\n\n" + diff[:3500]),
        payload={"resume_text": proposed},
        source="portfolio_agent",
        dedup_key=f"resume_evolve_{month}",
        reasoning="Master resume evolution (Part 5E): repo-evidenced bullets "
                  "only — ATS gap keywords are deliberately NOT inserted, "
                  "since the portfolio doesn't evidence them.",
        risk="write")
    return out.get("status")


# ---------------------------------------------------------------------------
# Application lifecycle (CAREER AUTOPILOT Part 5E) — wind-down on accepted
# ---------------------------------------------------------------------------

def _application_lifecycle_agent(events, ctx):
    """Subscribes to career.application_status_changed; an 'accepted'
    transition (the new terminal success status) proposes the goal
    wind-down bundle as ONE tier-2 approval — close the career goal (which
    is itself what deactivates JobScoutSensor + the career agents: they all
    no-op without an active career goal), archive open postings, and
    optionally draft withdrawal emails for other active applications (each
    of those re-parks as its own external-pinned send on execution).
    Nothing winds down silently."""

    def on_status_changed(ev):
        try:
            payload = ev.get("payload") or {}
            if payload.get("status") != "accepted":
                return
            application_id = payload.get("application_id") or ""
            goal_row = ctx.collab.conn.execute(
                "SELECT id, title, career_meta FROM goals WHERE domain='career'"
                " AND status='active' ORDER BY created_at DESC LIMIT 1").fetchone()
            goal_id = goal_row["id"] if goal_row else None
            # Part 5F ladder: a north-star role beyond the one just landed
            # means the wind-down PROMOTES instead of closing — same single
            # approval, the goal stays active and scouting re-aims.
            promote_to = None
            if goal_row and goal_row["career_meta"]:
                try:
                    meta = json.loads(goal_row["career_meta"])
                    ns = (meta.get("north_star_role") or "").strip()
                    if ns and ns.lower() != (meta.get("target_role") or "").strip().lower():
                        promote_to = ns
                except Exception:
                    promote_to = None
            open_postings = len(ctx.store.list_postings(
                ctx.user_id, status="discovered", limit=1000))
            others = [a for a in ctx.store.list_applications(ctx.user_id)
                      if a["id"] != application_id
                      and a["status"] in ("sent", "response", "interview", "offer")]

            from ..automation.executors import submit_action
            if promote_to:
                goal_step = (f"- promote the goal: '{promote_to}' becomes the "
                             "active target (goal stays open, scouting re-aims "
                             "at the north star)")
                title = (f"Offer accepted — promote the search to your north "
                         f"star ({promote_to})?")
            else:
                goal_step = (f"- close career goal: {goal_row['title']}" if goal_row
                             else "- no active career goal to close")
                title = "Offer accepted — wind down the job search?"
            steps = [goal_step,
                     f"- archive {open_postings} open posting(s)",
                     f"- propose withdrawal emails for {len(others)} other "
                     "active application(s) — each send parks as its own "
                     "tier-2 approval"]
            submit_action(
                ctx, 2, "career_wind_down",
                title=title,
                body="One approval executes every step:\n" + "\n".join(steps),
                payload={"goal_id": goal_id,
                         "accepted_application_id": application_id,
                         "withdraw_others": bool(others),
                         "promote_to_role": promote_to},
                source="application_lifecycle_agent",
                dedup_key=f"winddown_{goal_id or application_id}",
                reasoning=("The immediate role is landed; the ladder's next "
                           "rung takes over — nothing changes without this "
                           "approval." if promote_to else
                           "An accepted offer ends the search; continuing to "
                           "scout/apply now wastes everyone's time. Nothing "
                           "winds down without this approval."),
                risk="write")
        except Exception as exc:
            _report_error(events, "application_lifecycle", exc)

    events.subscribe("career.application_status_changed", on_status_changed)


# ---------------------------------------------------------------------------
# Interview debrief (CAREER AUTOPILOT Part 5E) — advisory, exactly once
# ---------------------------------------------------------------------------

_DEBRIEF_LOOKBACK_HOURS = 6


def _debrief_already_prompted(ctx, event_id: str) -> bool:
    row = ctx.collab.conn.execute(
        "SELECT value FROM prefs WHERE key=?",
        (f"debrief_prompted_{event_id}",)).fetchone()
    return row is not None


def _mark_debrief_prompted(ctx, event_id: str) -> None:
    ctx.collab.conn.execute(
        "INSERT OR IGNORE INTO prefs(key,value) VALUES(?,?)",
        (f"debrief_prompted_{event_id}", _dt.datetime.now(
            _dt.timezone.utc).isoformat()))
    ctx.collab.conn.commit()


def interview_debrief_check(events, ctx) -> int:
    """Part 5E: after a career-linked calendar event ENDS, prompt once
    (notification) for a quick debrief and pre-create the note skeleton
    (09_Memory/Interview Debrief - {company} - {date}) for the user to fill
    in — it feeds future prep packs (Part 5A-5C, when built) and
    preference drift. Advisory and skippable; the prefs-table guard makes
    it exactly-once per calendar event, never a re-prompt. Calendar is
    queried directly (mirroring meet_upcoming_meetings) because that tool
    only looks forward and this needs just-ended events."""
    if not config.agent_enabled("application_tracker"):
        return 0
    interviewing = [a for a in ctx.store.list_applications(ctx.user_id)
                    if a["status"] in ("interview", "offer")]
    if not interviewing:
        return 0
    creds = ctx.google_creds()
    if creds is None:
        return 0

    from ..career_inbound import _company_token
    companies = {}
    for app in interviewing:
        posting = ctx.store.get_posting(ctx.user_id, app["posting_id"]) or {}
        token = _company_token(posting.get("company") or "")
        if token:
            companies[token] = posting.get("company") or token

    now = _dt.datetime.now(_dt.timezone.utc)
    try:
        from googleapiclient.discovery import build
        svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        res = svc.events().list(
            calendarId="primary",
            timeMin=(now - _dt.timedelta(hours=_DEBRIEF_LOOKBACK_HOURS)).isoformat(),
            timeMax=now.isoformat(), maxResults=25, singleEvents=True,
            orderBy="startTime").execute()
        items = res.get("items", [])
    except Exception as exc:
        _report_error(events, "interview_debrief", exc)
        return 0

    prompted = 0
    for e in items:
        try:
            end_raw = (e.get("end") or {}).get("dateTime", "")
            try:
                end = _dt.datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            except Exception:
                continue   # all-day events don't "end" in a debriefable sense
            if end > now:
                continue   # still running
            event_id = e.get("id") or ""
            title = e.get("summary") or ""
            if not event_id or _debrief_already_prompted(ctx, event_id):
                continue
            title_low = title.lower()
            company = next((c for t, c in companies.items() if t in title_low),
                           None)
            if company is None:
                continue   # not career-linked — plain meetings stay meeting_prep's turf

            date_str = end.date().isoformat()
            try:
                from ..memory.writer import MemoryWriter
                from ..saas import tenancy
                vault = tenancy.resolve_vault_dir(ctx.user_id)
                vault.mkdir(parents=True, exist_ok=True)
                MemoryWriter(vault).write_atomic(
                    "interview debrief",
                    f"Interview Debrief - {company} - {date_str}",
                    (f"Interview: **{title}** ended {end_raw}\n\n"
                     "Fill in while it's fresh (this note feeds future prep "
                     "packs):\n\n- What they asked:\n- What went well:\n"
                     "- What to sharpen:\n- Next step / their timeline:\n"),
                    eid=f"debrief-{event_id}", tags=["career", "interview"])
            except Exception:
                pass   # the notification below is still the prompt
            try:
                ns = ctx.notify_store()
                ns.create(
                    type="career_interview_debrief",
                    title=f"Quick debrief? {company} interview just ended",
                    body=("A debrief note skeleton is in your vault "
                          f"(Interview Debrief - {company} - {date_str}) — "
                          "two minutes now beats a blank memory next round. "
                          "For a structured, searchable record, tell the "
                          "assistant about it (log_interview_from_chat) or "
                          "POST /api/career/interviews. Skippable; this is "
                          "the only prompt for this event."),
                    priority="normal",
                    related_entity={"entity_type": "calendar_event",
                                    "id": event_id})
            except Exception:
                pass
            _mark_debrief_prompted(ctx, event_id)
            _emit_insight(events, ctx, "interview_debrief",
                          f"Debrief prompted: {company}",
                          f"Career-linked calendar event '{title}' ended at "
                          f"{end_raw}; prompted once for a debrief.",
                          {"event_id": event_id, "company": company})
            prompted += 1
        except Exception as exc:
            _report_error(events, "interview_debrief", exc)
    return prompted


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


def _health_bootstrap_agent(events, ctx):
    """No-op subscription, same reasoning as _meeting_prep_agent/
    _portfolio_agent: LIFE AUTOPILOT L1's health bootstrap + vault re-parse
    have no natural triggering EVENT (finding a vault folder and noticing
    it changed are both poll-driven, not pushed) — exists only so
    'health_bootstrap' is visible/consistent in register_reactive_agents'
    list and honors its kill switch. Real logic: bootstrap_health_profile()
    / check_vault_reparse() (amy/life/bootstrap.py), called directly by the
    health_bootstrap_check job."""
    return


def _habit_signals_agent(events, ctx):
    """LIFE AUTOPILOT L4: real-time habit_links evaluation. Subscribes to
    context.place_entered (geo_place_visit links) and context.place_left
    (left_office_before links — 'left office by 6' checks the moment the
    place is left, not at day-close). Absence-type links (txn_absence/
    txn_presence/reading_minutes/sleep_window_met) have no real-time event
    to hang off and are day-close only — see evaluate_day_close(), driven
    by the life_metrics_daily job right after that day's row is computed.
    Never touches coordinates — only place_id/name/kind from the payload,
    same rail as _errand_agent/_budget_agent's spend_caution handler."""
    from ..life import habits as life_habits

    def on_place_entered(ev):
        try:
            life_habits.on_place_entered(ctx, events, ev.get("payload") or {})
        except Exception as exc:
            _report_error(events, "habit_signals", exc)

    def on_place_left(ev):
        try:
            life_habits.on_place_left(ctx, events, ev.get("payload") or {})
        except Exception as exc:
            _report_error(events, "habit_signals", exc)

    events.subscribe("context.place_entered", on_place_entered)
    events.subscribe("context.place_left", on_place_left)


_LIFE_INFERENCE_AGENT_NAMES = (
    "life_commute", "life_meals", "life_sleep", "life_activity", "life_reading",
    "life_meeting_load", "life_admin", "life_seasonal", "life_social",
)


def _life_agent_noop(events, ctx):
    """Shared no-op subscription for LIFE AUTOPILOT L3's nine inference
    agents (commute/meals/sleep/activity/reading/meeting_load/admin/
    seasonal/social) — none have a natural push event ('a weekly
    behavioral pattern changed' only exists by polling life_metrics), so
    all nine share this identical no-op body rather than nine near-
    duplicate copies. Each still gets its OWN kill switch and its own
    entry in register_reactive_agents' returned list (registered under a
    different name per call in the loop below). Real logic:
    amy/life/inference.py's per-agent check functions, driven weekly by
    the life_inference_scan job."""
    return


def _life_opportunity_agent(events, ctx):
    """LIFE AUTOPILOT L9: the ONE dispatcher for place-opportunity rules
    (amy/life/opportunity_rules.py). Subscribes to context.place_entered
    (dwell only — existing geo hysteresis already filters pass-bys).
    Never touches coordinates — only place_id/name/kind from the payload,
    same rail as _errand_agent/_habit_signals_agent."""
    from ..life import opportunity as life_opportunity

    def on_place_entered(ev):
        try:
            life_opportunity.dispatch(ctx, events, ev.get("payload") or {})
        except Exception as exc:
            _report_error(events, "life_opportunity", exc)

    events.subscribe("context.place_entered", on_place_entered)


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
    if config.agent_enabled("career_goal"):
        _once("career_goal", _career_goal_agent)
    if config.agent_enabled("portfolio"):
        _once("portfolio", _portfolio_agent)
    if config.agent_enabled("application_tracker"):
        _once("application_lifecycle", _application_lifecycle_agent)
    if config.agent_enabled("life_health"):
        _once("health_bootstrap", _health_bootstrap_agent)
    if config.agent_enabled("life_habits"):
        _once("habit_signals", _habit_signals_agent)
    for _name in _LIFE_INFERENCE_AGENT_NAMES:
        if config.agent_enabled(_name):
            _once(_name, _life_agent_noop)
    if config.agent_enabled("life_opportunity"):
        _once("life_opportunity", _life_opportunity_agent)
    return sorted(seen)
