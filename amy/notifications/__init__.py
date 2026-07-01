"""In-app notification system.

NotificationStore  — read/write notifications in per-user collab.db.
NotificationService — evaluates finance conditions and emits notification rows.
"""
from __future__ import annotations

import datetime as _dt
import json
import uuid


# ---------------------------------------------------------------------------
# Store (wraps the notifications table in collab.db)
# ---------------------------------------------------------------------------

class NotificationStore:
    def __init__(self, collab_db):
        self.db = collab_db

    def _now(self) -> str:
        return _dt.datetime.now(_dt.timezone.utc).isoformat()

    def create(self, type: str, title: str, body: str,
               priority: str = "normal",
               related_entity: dict | None = None) -> str:
        nid = uuid.uuid4().hex
        self.db.conn.execute(
            "INSERT INTO notifications(id,type,title,body,created_at,priority,related_entity)"
            " VALUES(?,?,?,?,?,?,?)",
            (nid, type, title, body, self._now(), priority,
             json.dumps(related_entity or {})))
        self.db.conn.commit()
        return nid

    def list(self, unread_only: bool = False, limit: int = 50) -> list[dict]:
        if unread_only:
            rows = self.db.conn.execute(
                "SELECT * FROM notifications WHERE read_at IS NULL"
                " ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = self.db.conn.execute(
                "SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["related_entity"] = json.loads(d.get("related_entity") or "{}")
            result.append(d)
        return result

    def mark_read(self, nid: str) -> bool:
        c = self.db.conn.execute(
            "UPDATE notifications SET read_at=? WHERE id=?",
            (self._now(), nid))
        self.db.conn.commit()
        return c.rowcount > 0

    def mark_all_read(self):
        self.db.conn.execute(
            "UPDATE notifications SET read_at=? WHERE read_at IS NULL",
            (self._now(),))
        self.db.conn.commit()

    def unread_count(self) -> int:
        return self.db.conn.execute(
            "SELECT COUNT(*) n FROM notifications WHERE read_at IS NULL"
        ).fetchone()["n"]

    def exists_today(self, type: str, related_id: str) -> bool:
        """Prevent duplicate notifications for the same event within 24h."""
        cutoff = (_dt.datetime.now(_dt.timezone.utc)
                  - _dt.timedelta(hours=24)).isoformat()
        row = self.db.conn.execute(
            "SELECT id FROM notifications"
            " WHERE type=? AND related_entity LIKE ? AND created_at>=? LIMIT 1",
            (type, f'%"{related_id}"%', cutoff)).fetchone()
        return row is not None


# ---------------------------------------------------------------------------
# Service — evaluates finance conditions and emits notifications
# ---------------------------------------------------------------------------

class NotificationService:
    """Reads FinanceEngine conditions and writes notification rows.

    Called from the digest scheduler so alerts are generated alongside the
    daily digest without a separate background job.
    """

    def __init__(self, store: NotificationStore):
        self.store = store

    def evaluate_finance(self, finance_engine) -> list[str]:
        """Evaluate all finance alert conditions. Returns list of created nids."""
        created: list[str] = []

        # 1 — budget overages
        for b in finance_engine.budget_status():
            if not b["over_budget"]:
                continue
            cat = b["category"]
            ref_id = f"budget_over_{cat}"
            if self.store.exists_today("budget_overage", ref_id):
                continue
            nid = self.store.create(
                type="budget_overage",
                title=f"Budget exceeded: {cat}",
                body=(
                    f"You've spent ₹{b['spent']:,.0f} in {cat} this month, "
                    f"which is ₹{abs(b['headroom']):,.0f} over your "
                    f"₹{b['limit']:,.0f} limit."
                ),
                priority="high",
                related_entity={"entity_type": "budget", "category": cat,
                                 "id": ref_id},
            )
            created.append(nid)

        # 2 — bills due in ≤ 3 days (high priority)
        for bill in finance_engine.upcoming_bills(days=3):
            ref_id = f"bill_{bill['id']}_3d"
            if self.store.exists_today("bill_due_soon", ref_id):
                continue
            nid = self.store.create(
                type="bill_due_soon",
                title=f"Bill due soon: {bill['name']}",
                body=(
                    f"{bill['name']} renews on {bill['renewal_date']} "
                    f"(₹{bill['monthly_cost']:,.0f}/mo). "
                    "Action required: ensure funds are available."
                ),
                priority="high",
                related_entity={"entity_type": "subscription",
                                 "id": bill["id"], "ref": ref_id},
            )
            created.append(nid)

        # 3 — bills due in 4–14 days (normal priority)
        bills_3d_ids = {b["id"] for b in finance_engine.upcoming_bills(days=3)}
        for bill in finance_engine.upcoming_bills(days=14):
            if bill["id"] in bills_3d_ids:
                continue  # already alerted as high-priority
            ref_id = f"bill_{bill['id']}_14d"
            if self.store.exists_today("bill_upcoming", ref_id):
                continue
            nid = self.store.create(
                type="bill_upcoming",
                title=f"Upcoming renewal: {bill['name']}",
                body=(
                    f"{bill['name']} renews on {bill['renewal_date']} "
                    f"(₹{bill['monthly_cost']:,.0f}/mo)."
                ),
                priority="normal",
                related_entity={"entity_type": "subscription",
                                 "id": bill["id"], "ref": ref_id},
            )
            created.append(nid)

        # 4 — multi-week spending pattern nudges
        created.extend(self._check_spending_patterns(finance_engine))

        return created

    def _check_spending_patterns(self, finance_engine) -> list[str]:
        """
        Detect categories that have been over-budget (or consistently high-spend)
        for 3 of the last 4 weeks and create a pattern_alert notification.

        Week buckets: rolling 7-day windows ending today.
          Week 0: today-6 → today
          Week 1: today-13 → today-7
          Week 2: today-20 → today-14
          Week 3: today-27 → today-21

        A category is flagged when:
          - It has a budget set AND weekly spend > budget/4 in ≥3 of last 4 weeks.
        """
        import datetime as _dt
        created: list[str] = []
        today = _dt.date.today()

        # Build 4 week windows: list of (since_str, until_str)
        windows: list[tuple[str, str]] = []
        for w in range(4):
            end   = (today - _dt.timedelta(days=w * 7)).isoformat()
            start = (today - _dt.timedelta(days=w * 7 + 6)).isoformat()
            windows.append((start, end))

        # Get budgeted categories
        budgets = {b["category"]: b["monthly_limit"]
                   for b in finance_engine.list_budgets()}
        if not budgets:
            return []

        # Accumulate weekly spend per category
        weekly_spend: dict[str, list[float]] = {cat: [] for cat in budgets}
        for since, until in windows:
            txns = finance_engine.list_transactions(limit=2000, since=since, until=until)
            week_by_cat: dict[str, float] = {}
            for t in txns:
                if t["amount"] < 0:
                    week_by_cat[t["category"]] = (
                        week_by_cat.get(t["category"], 0.0) + abs(t["amount"]))
            for cat in budgets:
                weekly_spend[cat].append(week_by_cat.get(cat, 0.0))

        for cat, limit in budgets.items():
            weekly_limit = limit / 4.0
            weeks_over = sum(1 for w in weekly_spend[cat] if w > weekly_limit)
            if weeks_over < 3:
                continue
            ref_id = f"pattern_{cat}"
            if self.store.exists_today("spending_pattern", ref_id):
                continue
            avg_weekly = round(sum(weekly_spend[cat]) / 4, 0)
            nid = self.store.create(
                type="spending_pattern",
                title=f"Spending pattern: {cat}",
                body=(
                    f"You've exceeded your weekly {cat} budget "
                    f"(₹{weekly_limit:,.0f}/week) in {weeks_over} of the last 4 weeks. "
                    f"Your average weekly spend is ₹{avg_weekly:,.0f}. "
                    f"Consider adjusting your budget or cutting back."
                ),
                priority="normal",
                related_entity={"entity_type": "pattern", "category": cat,
                                 "id": ref_id, "weeks_over": weeks_over},
            )
            created.append(nid)

        return created
