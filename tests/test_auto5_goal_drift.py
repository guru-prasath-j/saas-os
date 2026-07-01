"""Tests for Automation 5: Goal drift reports via ExecutiveAgent."""
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
def planner(collab_db):
    from amy.collab.planner import PlannerAgent
    return PlannerAgent(collab_db)


@pytest.fixture()
def executive(collab_db, finance_engine):
    from amy.autonomous.executive import ExecutiveAgent
    return ExecutiveAgent(collab_db, finance_db_path=str(finance_engine.path))


def _future(months: int) -> str:
    d = _dt.date.today() + _dt.timedelta(days=int(months * 30.44))
    return d.isoformat()


def _past_months(months: int) -> str:
    d = _dt.date.today() - _dt.timedelta(days=int(months * 30.44))
    return d.isoformat()


class TestFinanceTargetOnGoal:
    def test_set_and_get_finance_target(self, planner):
        gid = planner.create_goal("Buy laptop", domain="finance", target_date=_future(6))
        ok = planner.set_finance_target(gid, 80000.0, "Savings")
        assert ok is True
        target = planner.get_finance_target(gid)
        assert target is not None
        assert target["savings_target"] == 80000.0
        assert target["monthly_savings_category"] == "Savings"

    def test_get_finance_target_returns_none_when_not_set(self, planner):
        gid = planner.create_goal("Random goal")
        assert planner.get_finance_target(gid) is None

    def test_set_finance_target_unknown_goal_returns_false(self, planner):
        ok = planner.set_finance_target("nonexistent_id", 50000.0)
        assert ok is False

    def test_finance_meta_column_exists(self, collab_db):
        cols = [r[1] for r in collab_db.conn.execute(
            "PRAGMA table_info(goals)").fetchall()]
        assert "finance_meta" in cols


class TestGoalDriftAnalysis:
    def test_no_finance_goals_empty_report(self, executive):
        assert executive.analyze_finance_drift() == []

    def test_on_track_no_high_drift(self, planner, executive, finance_engine):
        """Goal with sufficient actual savings → high_drift=False."""
        gid = planner.create_goal("Emergency fund", domain="finance",
                                   target_date=_future(6))
        planner.set_finance_target(gid, 60000.0, "Savings")
        # required = 60000/6 = 10000/month
        # actual: 3 months of 15000 = 45000 / 3 = 15000/month
        for i in range(3):
            finance_engine.add_transaction(15000, "Savings",
                                           date=_past_months(i + 0.5))
        reports = executive.analyze_finance_drift()
        assert len(reports) == 1
        assert reports[0]["high_drift"] is False
        assert reports[0]["drift"] < 0  # actually AHEAD of schedule

    def test_behind_on_savings_high_drift(self, planner, executive, finance_engine):
        """Goal with insufficient savings → high_drift=True."""
        gid = planner.create_goal("Laptop fund", domain="finance",
                                   target_date=_future(6))
        planner.set_finance_target(gid, 60000.0, "Savings")
        # required = 10000/month; actual = 2000/month
        for i in range(3):
            finance_engine.add_transaction(2000, "Savings",
                                           date=_past_months(i + 0.5))
        reports = executive.analyze_finance_drift()
        assert len(reports) == 1
        r = reports[0]
        assert r["high_drift"] is True
        assert r["drift"] > 0.30

    def test_under_30pct_drift_no_alert(self, planner, executive, finance_engine):
        """Under 30% drift does not set high_drift."""
        gid = planner.create_goal("Bike", domain="finance", target_date=_future(10))
        planner.set_finance_target(gid, 30000.0, "Savings")
        # required ≈ 3000/month; save 2800/month → drift ≈ 6.7% — well under 30%
        for i in range(3):
            finance_engine.add_transaction(2800, "Savings",
                                           date=_past_months(i + 0.5))
        reports = executive.analyze_finance_drift()
        assert len(reports) == 1
        assert reports[0]["high_drift"] is False

    def test_no_savings_transactions_at_all(self, planner, executive, finance_engine):
        """Zero actual savings → max drift → high_drift=True."""
        gid = planner.create_goal("Wedding fund", domain="finance",
                                   target_date=_future(12))
        planner.set_finance_target(gid, 120000.0, "Savings")
        # No savings transactions at all
        reports = executive.analyze_finance_drift()
        assert len(reports) == 1
        assert reports[0]["high_drift"] is True
        assert reports[0]["actual_monthly"] == 0.0

    def test_goal_without_target_date_skipped(self, planner, executive):
        gid = planner.create_goal("Vague goal", domain="finance")
        planner.set_finance_target(gid, 50000.0, "Savings")
        reports = executive.analyze_finance_drift()
        assert len(reports) == 0  # no target_date → skip

    def test_goal_too_close_skipped(self, planner, executive, finance_engine):
        """Goals with < 0.5 months remaining are skipped."""
        gid = planner.create_goal("Imminent goal", domain="finance",
                                   target_date=(_dt.date.today() + _dt.timedelta(days=5)).isoformat())
        planner.set_finance_target(gid, 10000.0, "Savings")
        reports = executive.analyze_finance_drift()
        assert len(reports) == 0

    def test_report_structure(self, planner, executive, finance_engine):
        gid = planner.create_goal("Holiday", domain="finance", target_date=_future(8))
        planner.set_finance_target(gid, 40000.0, "Savings")
        reports = executive.analyze_finance_drift()
        assert len(reports) == 1
        r = reports[0]
        required_keys = {"goal_id", "goal_title", "savings_target", "category",
                         "months_remaining", "required_monthly", "actual_monthly",
                         "drift", "high_drift", "target_date"}
        assert required_keys.issubset(r.keys())

    def test_multiple_goals_multiple_reports(self, planner, executive, finance_engine):
        for i, (name, target, months) in enumerate([
            ("Fund A", 60000, 6),
            ("Fund B", 120000, 12),
        ]):
            gid = planner.create_goal(name, domain="finance",
                                       target_date=_future(months))
            planner.set_finance_target(gid, target, "Savings")
        reports = executive.analyze_finance_drift()
        assert len(reports) == 2

    def test_non_finance_domain_goal_skipped(self, planner, executive, finance_engine):
        """Goals without a finance_meta set are ignored."""
        gid = planner.create_goal("Fitness goal", domain="health",
                                   target_date=_future(6))
        reports = executive.analyze_finance_drift()
        assert len(reports) == 0

    def test_executive_brief_includes_drift(self, planner, executive, finance_engine):
        gid = planner.create_goal("Car fund", domain="finance", target_date=_future(6))
        planner.set_finance_target(gid, 60000.0, "Savings")
        brief = executive.brief()
        assert "finance_drift" in brief
        assert isinstance(brief["finance_drift"], list)

    def test_brief_without_finance_db_no_drift_key(self, collab_db):
        from amy.autonomous.executive import ExecutiveAgent
        agent = ExecutiveAgent(collab_db)  # no finance_db_path
        brief = agent.brief()
        assert "finance_drift" not in brief


class TestDriftNotificationsViaScheduler:
    def test_high_drift_creates_notification(self, collab_db, finance_engine, tmp_path):
        from amy.events.scheduler import generate_and_store
        from amy.notifications import NotificationStore
        from amy.collab.planner import PlannerAgent
        planner = PlannerAgent(collab_db)
        gid = planner.create_goal("Emergency fund", domain="finance",
                                   target_date=_future(6))
        planner.set_finance_target(gid, 60000.0, "Savings")
        # No savings transactions → 100% drift → high_drift

        generate_and_store(collab_db, finance_db_path=str(finance_engine.path))
        store = NotificationStore(collab_db)
        drift_notifs = [n for n in store.list() if n["type"] == "goal_drift"]
        assert len(drift_notifs) == 1
        assert drift_notifs[0]["priority"] == "high"
        assert "Emergency fund" in drift_notifs[0]["title"]

    def test_no_notification_when_on_track(self, collab_db, finance_engine):
        from amy.events.scheduler import generate_and_store
        from amy.notifications import NotificationStore
        from amy.collab.planner import PlannerAgent
        planner = PlannerAgent(collab_db)
        gid = planner.create_goal("Savings goal", domain="finance",
                                   target_date=_future(6))
        planner.set_finance_target(gid, 60000.0, "Savings")
        # On track: 15000/month for 3 months, required=10000
        for i in range(3):
            finance_engine.add_transaction(15000, "Savings",
                                           date=_past_months(i + 0.5))

        generate_and_store(collab_db, finance_db_path=str(finance_engine.path))
        store = NotificationStore(collab_db)
        drift_notifs = [n for n in store.list() if n["type"] == "goal_drift"]
        assert len(drift_notifs) == 0

    def test_drift_notification_deduped_within_24h(self, collab_db, finance_engine):
        from amy.events.scheduler import generate_and_store
        from amy.notifications import NotificationStore
        from amy.collab.planner import PlannerAgent
        planner = PlannerAgent(collab_db)
        gid = planner.create_goal("Holiday", domain="finance", target_date=_future(8))
        planner.set_finance_target(gid, 80000.0, "Savings")

        generate_and_store(collab_db, finance_db_path=str(finance_engine.path))
        generate_and_store(collab_db, finance_db_path=str(finance_engine.path))
        store = NotificationStore(collab_db)
        drift_notifs = [n for n in store.list() if n["type"] == "goal_drift"]
        assert len(drift_notifs) == 1  # deduped on second run
