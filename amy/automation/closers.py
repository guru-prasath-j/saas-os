"""Bigger scheduled workflows: monthly close, custodial autopilot,
morning briefing, and the daily Autopilot run (Phases 3–4).
"""
from __future__ import annotations

import calendar
import datetime as _dt

from .executors import JobCtx, submit_action
from . import learning

_SUB_SUGGESTION_MIN_CONFIDENCE = 0.7


# ---------------------------------------------------------------------------
# Monthly close — runs on the 1st, reports on the month that just ended
# ---------------------------------------------------------------------------

def monthly_close(ctx: JobCtx) -> dict:
    from ..finance.categorizer import auto_categorize_all
    from ..finance.subscription_detect import detect_subscriptions

    today = _dt.date.today()
    first_this = today.replace(day=1)
    last_month_end = first_this - _dt.timedelta(days=1)
    month_start = last_month_end.replace(day=1)
    month_label = month_start.strftime("%B %Y")

    fe = ctx.open_finance()
    report_lines: list[str] = []
    result: dict = {"month": month_label}
    try:
        # 1 — clean categorisation first so the numbers below are honest
        learned = learning.apply_learned_rules(fe)
        recategorized = auto_categorize_all(fe, llm=ctx.llm)
        result["recategorized"] = learned + recategorized

        # 2 — budget vs actual for the closed month
        spent_by_cat: dict[str, float] = {}
        for t in fe.list_transactions(limit=5000,
                                      since=month_start.isoformat(),
                                      until=last_month_end.isoformat()):
            if (t["amount"] or 0) < 0:
                spent_by_cat[t["category"]] = (
                    spent_by_cat.get(t["category"], 0.0) + abs(t["amount"]))
        budgets = {b["category"]: b["monthly_limit"] for b in fe.list_budgets()}
        overages = []
        for cat, limit in budgets.items():
            spent = spent_by_cat.get(cat, 0.0)
            if spent > limit:
                overages.append(f"{cat}: ₹{spent:,.0f} / ₹{limit:,.0f}")
        total_spend = sum(spent_by_cat.values())
        report_lines.append(f"Total spend in {month_label}: ₹{total_spend:,.0f}.")
        report_lines.append(
            ("Over budget — " + "; ".join(overages)) if overages
            else "All budgets held.")
        result["total_spend"] = round(total_spend, 2)
        result["overages"] = len(overages)

        # 3 — newly detected subscriptions → Approval Inbox (tier 2)
        proposed_subs = 0
        try:
            for s in detect_subscriptions(fe, ctx.llm):
                if (s.get("confidence") or 0) < _SUB_SUGGESTION_MIN_CONFIDENCE:
                    continue
                r = submit_action(
                    ctx, tier=2, action_type="add_subscription",
                    title=f"Track new subscription: {s['name']}",
                    body=(f"₹{s['amount']:,.0f}/{s.get('billing_cycle', 'monthly')} "
                          f"seen {s.get('occurrences', '?')}× (last {s.get('last_date')}). "
                          f"Confidence {s.get('confidence', 0):.0%}."),
                    payload={"name": s["name"], "monthly_cost": s["amount"],
                             "renewal_date": s.get("next_due")},
                    source="monthly_close",
                    dedup_key=f"sub_{s['name'].lower()}_{today.strftime('%Y-%m')}")
                if r["status"] == "pending":
                    proposed_subs += 1
        except Exception:
            pass
        if proposed_subs:
            report_lines.append(
                f"{proposed_subs} new subscription(s) detected — awaiting approval.")
        result["subscriptions_proposed"] = proposed_subs

        # 4 — business entities: refresh compliance suggestions
        compliance_new = 0
        try:
            from ..finance.business.compliance import generate_suggestions
            for entity in fe.list_business_entities():
                try:
                    compliance_new += len(generate_suggestions(fe, entity, ctx.llm))
                except Exception:
                    pass
        except Exception:
            pass
        if compliance_new:
            report_lines.append(
                f"{compliance_new} new compliance suggestion(s) across business entities.")
        result["compliance_suggestions"] = compliance_new
    finally:
        fe.close()

    # 5 — publish the CFO report
    body = " ".join(report_lines)
    try:
        ns = ctx.notify_store()
        ref = f"close_{month_start.strftime('%Y-%m')}"
        if not ns.exists_today("monthly_close", ref):
            ns.create(type="monthly_close",
                      title=f"Monthly CFO report — {month_label}",
                      body=body, priority="normal",
                      related_entity={"entity_type": "report", "id": ref})
    except Exception:
        pass
    try:
        from ..collab.memory import MemoryManager
        MemoryManager(ctx.collab).add_summary(
            f"Monthly close ({month_label}): {body}")
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Custodial autopilot — proposal is prepared; the user only taps approve
# ---------------------------------------------------------------------------

def custodial_autopilot(ctx: JobCtx) -> dict:
    from ..finance.custodial import next_cycle_prefill, run_validation

    today = _dt.date.today().isoformat()
    fe = ctx.open_finance()
    proposed = 0
    try:
        for acc in fe.list_accounts():
            if acc.get("account_type") != "custodial":
                continue
            prefill = next_cycle_prefill(fe, acc["id"])
            due = prefill.get("due_date")
            if not due or due > today:
                continue
            disbursements = [
                {"beneficiary_id": b["beneficiary_id"], "name": b["name"],
                 "amount": b["last_amount"]}
                for b in prefill["beneficiaries"] if b.get("last_amount")
            ]
            if not disbursements:
                continue
            total = sum(d["amount"] for d in disbursements)
            issues = run_validation(fe, acc["id"]).get("issues", [])
            warn = f" ⚠ {len(issues)} validation issue(s)." if issues else ""
            names = ", ".join(f"{d['name']} ₹{d['amount']:,.0f}" for d in disbursements)
            r = submit_action(
                ctx, tier=2, action_type="custodial_disburse",
                title=f"Custodial cycle due ({acc.get('nickname') or acc.get('bank_name')})",
                body=(f"Due {due}. Prefilled from last cycle: {names} "
                      f"(total ₹{total:,.0f}).{warn} Approve after sending the "
                      "transfers — this records them and updates the Sheet."),
                payload={"account_id": acc["id"], "disbursements": disbursements},
                source="custodial_autopilot",
                dedup_key=f"custodial_{acc['id']}_{due}")
            if r["status"] == "pending":
                proposed += 1
    finally:
        fe.close()
    return {"cycles_proposed": proposed}


# ---------------------------------------------------------------------------
# "project_pulse" — Work section folded into the morning briefing
# (CONNECTOR COMPLETION Part 2: explicitly NOT a competing briefing — a
# provider function morning_briefing() calls, same shape as the obligations/
# deadlines sections above it)
# ---------------------------------------------------------------------------

def _work_section(ctx: JobCtx) -> list[str]:
    """PRs awaiting review, Plane tasks due within 48h, today's meetings.
    Every piece is independently best-effort: no GitHub/Plane connector
    registered, or Google Calendar not linked, just omits that piece —
    never breaks the rest of the briefing. Read tools only (RISK_READ), so
    actor="agent" here doesn't route through the approval gate."""
    from .. import tools
    from ..connectors.mcp_call import extract_list

    lines: list[str] = []

    try:
        prs = extract_list(tools.invoke(ctx, "github_list_prs", {}, actor="agent"))
        awaiting = [p for p in prs if p.get("requested_reviewers") or p.get("reviewers")]
        if awaiting:
            titles = "; ".join(str(p.get("title") or f"PR #{p.get('number')}")
                               for p in awaiting[:4])
            lines.append(f"PRs awaiting your review: {titles}.")
    except Exception:
        pass

    try:
        tasks = extract_list(tools.invoke(ctx, "plane_list_tasks", {}, actor="agent"))
        cutoff = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=48)
        due_soon = []
        for t in tasks:
            due = t.get("due_date") or t.get("target_date")
            if not due:
                continue
            try:
                due_dt = _dt.datetime.fromisoformat(str(due))
                if due_dt.tzinfo is None:
                    due_dt = due_dt.replace(tzinfo=_dt.timezone.utc)
            except Exception:
                continue
            if due_dt <= cutoff:
                due_soon.append(t)
        if due_soon:
            titles = "; ".join(str(t.get("name") or t.get("title") or t.get("id"))
                               for t in due_soon[:4])
            lines.append(f"Plane tasks due within 48h: {titles}.")
    except Exception:
        pass

    try:
        meetings = (tools.invoke(ctx, "meet_upcoming_meetings", {"hours": 24},
                                 actor="agent") or {}).get("meetings", [])
        if meetings:
            titles = "; ".join(f"{m.get('title') or '(no title)'} ({m.get('start', '')})"
                               for m in meetings[:4])
            lines.append(f"Today's meetings: {titles}.")
    except Exception:
        pass

    lines.extend(_career_briefing_lines(ctx))

    return ["Work: " + " ".join(lines)] if lines else []


def _life_section(ctx: JobCtx) -> list[str]:
    """LIFE AUTOPILOT L6: today's auto-checks, streaks, ONE pattern
    insight max, admin deadlines (commitments — not otherwise surfaced
    anywhere in this briefing), and L8/L9 signals. Every piece
    independently best-effort, same idiom as _work_section."""
    from .. import config
    if config._env("AMY_LIFE_AUTOPILOT", "true").strip().lower() in ("0", "false", "no", "off"):
        return []

    lines: list[str] = []
    today_s = _dt.date.today().isoformat()

    try:
        n = ctx.collab.conn.execute(
            "SELECT COUNT(*) c FROM events WHERE type='life.habit_autocompleted'"
            " AND substr(ts,1,10)=?", (today_s,)).fetchone()["c"]
        if n:
            lines.append(f"{n} habit(s) auto-tracked today.")
    except Exception:
        pass

    try:
        from ..life.habits import streak_with_grace
        habits = ctx.open_habits()
        try:
            best = None
            for h in habits.list_habits():
                s = streak_with_grace(ctx, h["id"], habits)
                if s and (best is None or s > best[1]):
                    best = (h["title"], s)
        finally:
            habits.close()
        if best and best[1] >= 3:
            lines.append(f"Longest streak: {best[0]} ({best[1]} days).")
    except Exception:
        pass

    try:
        row = ctx.collab.conn.execute(
            "SELECT payload FROM events WHERE type='life.pattern_detected'"
            " ORDER BY ts DESC LIMIT 1").fetchone()
        if row:
            import json as _json
            p = _json.loads(row["payload"] or "{}")
            if p.get("agent"):
                lines.append(f"Pattern noticed: {p['agent']} ({p.get('pattern_key', '')}).")
    except Exception:
        pass

    try:
        fe = ctx.open_finance()
        try:
            from ..commitments import CommitmentEngine
            due_soon = [c for c in CommitmentEngine(fe).list("open") if c["days_left"] <= 3]
        finally:
            fe.close()
        if due_soon:
            titles = "; ".join(f"{c['title']} ({c['days_left']}d)" for c in due_soon[:3])
            lines.append(f"Commitments due soon: {titles}.")
    except Exception:
        pass

    try:
        yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
        y = ctx.store.get_life_metrics(ctx.user_id, yesterday)
        if y and y.get("meal_captures"):
            lines.append(f"{y['meal_captures']} meal capture(s) logged yesterday.")
    except Exception:
        pass

    try:
        n = ctx.collab.conn.execute(
            "SELECT COUNT(*) c FROM notifications WHERE type LIKE 'life_opp_%'"
            " AND substr(created_at,1,10)=?", (today_s,)).fetchone()["c"]
        if n:
            lines.append(f"{n} place-opportunity nudge(s) today.")
    except Exception:
        pass

    return ["Life: " + " ".join(lines)] if lines else []


def _career_briefing_lines(ctx: JobCtx) -> list[str]:
    """CAREER AUTOPILOT Part 4/6: high-match jobs discovered in the last
    24h, application status changes, a stall nudge, and the next milestone
    due. Reads job_postings/applications/goals/notifications/milestones
    directly (already cached by the job_scout/application-tracker/
    career_goal_stall_check jobs) — no live MCP call from the briefing
    itself. Independently best-effort like every other piece above: no
    active career goal just omits these lines."""
    out: list[str] = []
    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=24)).isoformat()

    try:
        threshold = _career_match_threshold()
        rows = ctx.collab.conn.execute(
            "SELECT title, company, match_score FROM job_postings"
            " WHERE uid=? AND discovered_at>=? AND match_score>=?"
            " ORDER BY match_score DESC LIMIT 4",
            (ctx.user_id, cutoff, threshold)).fetchall()
        if rows:
            items = "; ".join(f"{r['title']} at {r['company']} ({r['match_score']:.0f}/100 est.)"
                              for r in rows)
            out.append(f"New high-match jobs: {items}.")
    except Exception:
        pass

    try:
        rows = ctx.collab.conn.execute(
            "SELECT type, payload FROM events"
            " WHERE type LIKE 'career.application_%' AND ts>=? ORDER BY ts DESC LIMIT 4",
            (cutoff,)).fetchall()
        if rows:
            import json as _json
            statuses = []
            for r in rows:
                try:
                    p = _json.loads(r["payload"] or "{}")
                except Exception:
                    p = {}
                label = r["type"].rsplit(".", 1)[-1].replace("_", " ")
                if p.get("status"):
                    label = f"{label} ({p['status']})"
                statuses.append(label)
            out.append(f"Application updates: {'; '.join(statuses)}.")
    except Exception:
        pass

    try:
        stall = ctx.collab.conn.execute(
            "SELECT title, body FROM notifications"
            " WHERE type='career_stall' AND read_at IS NULL"
            " ORDER BY created_at DESC LIMIT 1").fetchone()
        if stall:
            out.append(f"Stalled: {stall['title']}.")
    except Exception:
        pass

    try:
        goal = ctx.collab.conn.execute(
            "SELECT id FROM goals WHERE domain='career' AND status='active'"
            " ORDER BY created_at DESC LIMIT 1").fetchone()
        if goal:
            ms = ctx.collab.conn.execute(
                "SELECT title FROM milestones WHERE goal_id=? AND done=0"
                " ORDER BY position LIMIT 1", (goal["id"],)).fetchone()
            if ms:
                out.append(f"Next milestone: {ms['title']}.")
    except Exception:
        pass

    return out


def _career_match_threshold() -> float:
    from .. import config
    try:
        return float(config._env("AMY_CAREER_MATCH_THRESHOLD", "70"))
    except ValueError:
        return 70.0


# ---------------------------------------------------------------------------
# Morning briefing — one daily message that closes the loop
# ---------------------------------------------------------------------------

def morning_briefing(ctx: JobCtx) -> dict:
    """R5: locale-rendered, jurisdiction-aware daily briefing — money in the
    user's base currency (multi-currency breakdown when present), upcoming
    deadlines across ALL active jurisdictions, obligation statuses, agent
    insights from the last day, approvals, goals, seasonal awareness."""
    from ..notifications.email import send_email, smtp_configured

    today = _dt.date.today()
    today_s = today.isoformat()
    lines: list[str] = []

    # locale + packs from ctx (home first)
    jurisdictions = ctx._extras.get("jurisdictions") or ["india"]
    from ..jurisdictions import load_pack, upcoming_deadlines, PackError
    try:
        home = load_pack(jurisdictions[0])
    except PackError:
        home = load_pack("india")
    currency = home["currency"]
    from ..locale_fmt import format_money
    _m = lambda v: format_money(v, currency, decimals=0)   # noqa: E731

    # 1 — money (base currency; per-jurisdiction breakdown when multi)
    fe = ctx.open_finance()
    try:
        try:
            from ..fx import FxConverter, multi_currency_summary
            from ..saas import paths as _paths
            summary = multi_currency_summary(
                fe, currency["code"], jurisdictions[0],
                FxConverter(cache_dir=_paths.SAAS_DATA))
            month_out = sum(b.get("month_out", 0)
                            for b in summary["by_jurisdiction_in_base"].values())
            lines.append(f"Balance est. {_m(summary['balance_estimate_base'])}; "
                         f"this month's spend {_m(month_out)}.")
            juris_bits = [f"{jid}: {_m(b['balance'])}"
                          for jid, b in summary["by_jurisdiction_in_base"].items()]
            if len(juris_bits) > 1:
                lines.append("By jurisdiction — " + "; ".join(juris_bits) + ".")
        except Exception:
            spent = sum(fe.this_month_spend().values())
            lines.append(f"Balance est. {_m(fe.balance_estimate())}; "
                         f"this month's spend {_m(spent)}.")

        # 2 — obligations (R7A-2)
        try:
            from ..obligations import all_statuses
            for st in all_statuses(fe, today):
                if st.get("state") == "accruing" and st.get("estimated_liability"):
                    lines.append(
                        f"{st['name']}: est. liability "
                        f"{st['currency']} {st['estimated_liability']:,.0f}, "
                        f"due {st.get('next_due')}.")
                elif st.get("state") == "scheduled" and st.get("amount_due_by_next"):
                    lines.append(
                        f"{st['name']}: {st['currency']} "
                        f"{st['amount_due_by_next']:,.0f} due by {st.get('next_due')} "
                        f"({st.get('next_label')}).")
                elif st.get("state") == "needs_estimate":
                    lines.append(f"{st['name']}: set an annual estimate to "
                                 "get installment figures.")
        except Exception:
            pass

        # 3 — renewals in the next 7 days
        try:
            bills = fe.upcoming_bills(days=7)
            if bills:
                lines.append("Renewals this week: " + "; ".join(
                    f"{b['name']} ({_m(b['monthly_cost'])}, {b['renewal_date']})"
                    for b in bills[:4]) + ".")
        except Exception:
            pass
    finally:
        fe.close()

    # 4 — deadlines across ALL active jurisdictions (R7B)
    try:
        dls = []
        for jid in jurisdictions:
            try:
                dls.extend(upcoming_deadlines(load_pack(jid), after=today,
                                              horizon_days=30))
            except PackError:
                continue
        dls.sort(key=lambda d: d["date"])
        if dls:
            lines.append("Deadlines: " + "; ".join(
                f"{d['name']} in {d['jurisdiction']} in {d['days_away']} day(s)"
                for d in dls[:4]) + ".")
    except Exception:
        pass

    # 5 — agent insights from the last 24h (R2)
    try:
        cutoff = (_dt.datetime.now(_dt.timezone.utc)
                  - _dt.timedelta(hours=24)).isoformat()
        rows = ctx.collab.conn.execute(
            "SELECT payload FROM events WHERE type='agent.insight' AND ts>=?"
            " ORDER BY ts DESC LIMIT 3", (cutoff,)).fetchall()
        if rows:
            import json as _json
            summaries = [(_json.loads(r["payload"] or "{}")).get("summary", "")
                         for r in rows]
            lines.append("Agent insights: " + "; ".join(s for s in summaries if s) + ".")
    except Exception:
        pass

    # 5.5 — Work: PRs/tasks/meetings ("project_pulse", CONNECTOR COMPLETION Part 2)
    try:
        lines.extend(_work_section(ctx))
    except Exception:
        pass

    # 5.6 — Life: today's auto-checks, streaks, one pattern insight max,
    # admin deadlines, L8/L9 signals (LIFE AUTOPILOT L6)
    try:
        lines.extend(_life_section(ctx))
    except Exception:
        pass

    # 6 — approvals + goals + unread
    pending = ctx.store.list_approvals("pending", limit=5)
    if pending:
        lines.append(f"{ctx.store.pending_count()} approval(s) waiting: "
                     + "; ".join(p["title"] for p in pending) + ".")
    else:
        lines.append("No approvals waiting.")
    try:
        from ..autonomous import ExecutiveAgent
        brief = ExecutiveAgent(ctx.collab, llm=None,
                               finance_db_path=ctx.finance_path)
        prios = brief.prioritize_goals()[:3]
        if prios:
            lines.append("Top goals: "
                         + "; ".join(p["title"] for p in prios) + ".")
        conflicts = brief.resolve_conflicts()
        if conflicts:
            lines.append(f"{len(conflicts)} goal conflict(s) need attention.")
    except Exception:
        pass

    # 7 — pack-defined seasonal awareness
    try:
        for jid in jurisdictions:
            try:
                pack = load_pack(jid)
            except PackError:
                continue
            for note in pack.get("seasonal_notes", []):
                if today.month in (note.get("months") or []):
                    lines.append(f"[{pack['name']}] {note['note']}")
                elif note.get("hijri_months"):
                    from ..calendars import get_calendar
                    h = get_calendar("hijri")
                    hm = int(h.to_display(today).split("-")[1])
                    if hm in note["hijri_months"]:
                        lines.append(f"[{pack['name']}] {note['note']}")
    except Exception:
        pass

    ns = ctx.notify_store()
    try:
        unread = ns.unread_count()
        if unread:
            lines.append(f"{unread} unread notification(s).")
    except Exception:
        pass

    body = " ".join(lines)
    ref = f"briefing_{today_s}"
    created = None
    if not ns.exists_today("morning_briefing", ref):
        created = ns.create(type="morning_briefing",
                            title=f"Morning briefing — {today_s}",
                            body=body, priority="normal",
                            related_entity={"entity_type": "briefing", "id": ref})
        if smtp_configured() and ctx.user_email:
            send_email(ctx.user_email, f"[Amy] Morning briefing — {today_s}", body)
        try:
            eid = ctx.events().emit("digest.generated",
                                    {"summary": body, "kind": "morning_briefing"},
                                    source="briefing")
            from ..agents.reactive import _journal
            _journal(ctx, {"id": eid, "type": "digest.generated",
                           "payload": {"summary": body}, "ts": None,
                           "source": "briefing"})
        except Exception:
            pass   # fire-and-forget; the notification is already the record
    return {"created": bool(created), "summary": body}


# ---------------------------------------------------------------------------
# Daily Autopilot — the existing additive-only engine, finally on a schedule
# ---------------------------------------------------------------------------

def autopilot_run(ctx: JobCtx) -> dict:
    from ..autonomous import Autopilot

    ap = Autopilot(ctx.collab, llm=ctx.llm, events=ctx.events(),
                   finance_db_path=ctx.finance_path)
    out = ap.run(dry_run=False)
    return {"actions": out.get("count", 0)}
