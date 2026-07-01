"""Tests for Automation 3: Unused subscription flagging via Autopilot."""
from __future__ import annotations

import datetime as _dt

import pytest


@pytest.fixture()
def collab_db(tmp_path):
    from amy.collab.db import CollabDB
    db = CollabDB(str(tmp_path / "collab.db"))
    yield db
    db.close()


@pytest.fixture()
def finance_engine(tmp_path):
    from amy.finance.engine import FinanceEngine
    fe = FinanceEngine(str(tmp_path / "finance.db"))
    yield fe
    fe.close()


@pytest.fixture()
def autopilot(collab_db, finance_engine):
    from amy.autonomous.autopilot import Autopilot
    return Autopilot(collab_db, finance_db_path=str(finance_engine.path))


def _date(days_ago: int) -> str:
    return (_dt.date.today() - _dt.timedelta(days=days_ago)).isoformat()


class TestUnusedSubscriptionFlagging:
    def test_no_subscriptions_no_actions(self, autopilot, finance_engine):
        result = autopilot.run(dry_run=True)
        finance_actions = [a for a in result["actions"]
                           if a.get("action") == "flag_unused_subscription"]
        assert finance_actions == []

    def test_unused_sub_flagged_in_dry_run(self, autopilot, collab_db, finance_engine):
        finance_engine.add_subscription("Netflix", monthly_cost=499, status="active")
        # No matching transactions
        result = autopilot.run(dry_run=True)
        fin_actions = [a for a in result["actions"]
                       if a.get("action") == "flag_unused_subscription"]
        assert len(fin_actions) == 1
        assert "Netflix" in fin_actions[0]["target"]

    def test_used_sub_not_flagged(self, autopilot, finance_engine):
        finance_engine.add_subscription("Spotify", monthly_cost=119, status="active")
        # Transaction with "Spotify" in merchant within last 60 days
        finance_engine.add_transaction(-119, "Entertainment",
                                       merchant="Spotify Premium", date=_date(5))
        result = autopilot.run(dry_run=True)
        fin_actions = [a for a in result["actions"]
                       if a.get("action") == "flag_unused_subscription"]
        assert fin_actions == []

    def test_match_by_token_not_exact(self, autopilot, finance_engine):
        """'Amazon Prime' should match 'Amazon' in transaction merchant."""
        finance_engine.add_subscription("Amazon Prime", monthly_cost=299, status="active")
        finance_engine.add_transaction(-299, "Shopping",
                                       merchant="Amazon order", date=_date(10))
        result = autopilot.run(dry_run=True)
        fin_actions = [a for a in result["actions"]
                       if a.get("action") == "flag_unused_subscription"]
        assert fin_actions == []  # "amazon" matched

    def test_transaction_too_old_still_flagged(self, autopilot, finance_engine):
        """Transaction older than 60 days doesn't count as 'used'."""
        finance_engine.add_subscription("Netflix", monthly_cost=499, status="active")
        finance_engine.add_transaction(-499, "Entertainment",
                                       merchant="Netflix subscription", date=_date(65))
        result = autopilot.run(dry_run=True)
        fin_actions = [a for a in result["actions"]
                       if a.get("action") == "flag_unused_subscription"]
        assert len(fin_actions) == 1

    def test_cap_at_3_unused_flags(self, autopilot, finance_engine):
        """At most 3 unused subscriptions are flagged per run."""
        for name in ["Netflix", "Hotstar", "AppleTV", "SonyLIV", "Zee5"]:
            finance_engine.add_subscription(name, monthly_cost=299, status="active")
        result = autopilot.run(dry_run=True)
        fin_actions = [a for a in result["actions"]
                       if a.get("action") == "flag_unused_subscription"]
        assert len(fin_actions) <= 3

    def test_free_sub_not_flagged(self, autopilot, finance_engine):
        """Subscriptions with monthly_cost=0 (free tier) are not flagged."""
        finance_engine.add_subscription("GitHub Free", monthly_cost=0, status="active")
        result = autopilot.run(dry_run=True)
        fin_actions = [a for a in result["actions"]
                       if a.get("action") == "flag_unused_subscription"]
        assert fin_actions == []

    def test_inactive_sub_not_flagged(self, autopilot, finance_engine):
        finance_engine.add_subscription("Paused", monthly_cost=99, status="inactive")
        result = autopilot.run(dry_run=True)
        fin_actions = [a for a in result["actions"]
                       if a.get("action") == "flag_unused_subscription"]
        assert fin_actions == []

    def test_live_run_creates_goal_and_task(self, autopilot, collab_db, finance_engine):
        """Non-dry-run creates a Finance Review goal + task in collab.db."""
        finance_engine.add_subscription("Hotstar", monthly_cost=299, status="active")
        result = autopilot.run(dry_run=False)
        fin_actions = [a for a in result["actions"]
                       if a.get("action") == "flag_unused_subscription"]
        assert len(fin_actions) == 1

        # A Finance Review goal should exist
        goal = collab_db.conn.execute(
            "SELECT * FROM goals WHERE domain='finance' AND title='Finance Review'"
        ).fetchone()
        assert goal is not None

        # A task referencing Hotstar should exist
        tasks = collab_db.conn.execute(
            "SELECT title FROM tasks WHERE goal_id=?", (goal["id"],)
        ).fetchall()
        task_titles = [t["title"] for t in tasks]
        assert any("Hotstar" in t for t in task_titles)

    def test_live_run_emits_action_taken_event(self, autopilot, collab_db, finance_engine):
        finance_engine.add_subscription("Crunchyroll", monthly_cost=199, status="active")
        autopilot.run(dry_run=False)
        events = collab_db.conn.execute(
            "SELECT payload FROM events WHERE type='action.taken'"
        ).fetchall()
        payloads = [e["payload"] for e in events]
        assert any("Crunchyroll" in p for p in payloads)

    def test_finance_review_goal_reused_across_runs(self, autopilot, collab_db, finance_engine):
        """Second run reuses the same Finance Review goal, doesn't create a new one."""
        finance_engine.add_subscription("Netflix", monthly_cost=499, status="active")
        autopilot.run(dry_run=False)
        finance_engine.add_subscription("Hotstar", monthly_cost=299, status="active")
        autopilot.run(dry_run=False)

        goal_count = collab_db.conn.execute(
            "SELECT COUNT(*) c FROM goals WHERE domain='finance' AND title='Finance Review'"
        ).fetchone()["c"]
        assert goal_count == 1  # same goal reused

    def test_action_contains_monthly_cost(self, autopilot, finance_engine):
        finance_engine.add_subscription("Zee5", monthly_cost=149, status="active")
        result = autopilot.run(dry_run=True)
        fin_actions = [a for a in result["actions"]
                       if a.get("action") == "flag_unused_subscription"]
        assert fin_actions[0]["monthly_cost"] == 149
