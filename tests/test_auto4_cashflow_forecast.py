"""Tests for Automation 4: Cash-flow forecast via PredictiveEngine."""
from __future__ import annotations

import datetime as _dt

import pytest


@pytest.fixture()
def finance_engine(tmp_path):
    from amy.finance.engine import FinanceEngine
    fe = FinanceEngine(str(tmp_path / "finance.db"))
    yield fe
    fe.close()


@pytest.fixture()
def collab_db(tmp_path):
    from amy.collab.db import CollabDB
    db = CollabDB(str(tmp_path / "collab.db"))
    yield db
    db.close()


def _date(days_ago: int) -> str:
    return (_dt.date.today() - _dt.timedelta(days=days_ago)).isoformat()


def _spend(fe, amount: float, days_ago: int, category: str = "Shopping"):
    fe.add_transaction(-amount, category, date=_date(days_ago))


class TestCashFlowForecast:
    def test_no_data_returns_flat_no_alert(self, finance_engine):
        from amy.engines.predictive_engine import PredictiveEngine
        fc = PredictiveEngine(None).forecast_finance(finance_engine)
        assert fc["metric"] == "cash_flow"
        assert fc["trend"] == "flat"
        assert fc["alert"] is False
        assert fc["this_week_spend"] == 0
        assert fc["prev_week_spend"] == 0

    def test_alert_when_projected_exceeds_comfortable(self, finance_engine):
        """When projected spend > income/4 × 1.1, alert=True."""
        from amy.engines.predictive_engine import PredictiveEngine
        finance_engine.add_income_source("Salary", amount=40000)
        # comfortable_weekly = 40000/4 * 1.1 = 11000
        # spend heavily this week so projection is high
        for d in range(1, 7):
            _spend(finance_engine, 3000, d)       # this week: 18000
        for d in range(8, 14):
            _spend(finance_engine, 1000, d)       # prev week: 6000
        # projected = 18000 + (18000-6000) = 30000 > 11000
        fc = PredictiveEngine(None).forecast_finance(finance_engine)
        assert fc["alert"] is True
        assert fc["trend"] == "up"
        assert "Projected" in fc["note"]

    def test_no_alert_when_under_budget(self, finance_engine):
        finance_engine.add_income_source("Salary", amount=40000)
        # comfortable_weekly = 11000; spend 2000/week both weeks
        for d in range(1, 7):
            _spend(finance_engine, 300, d)
        for d in range(8, 14):
            _spend(finance_engine, 300, d)

        from amy.engines.predictive_engine import PredictiveEngine
        fc = PredictiveEngine(None).forecast_finance(finance_engine)
        assert fc["alert"] is False

    def test_trend_down_when_spend_decreasing(self, finance_engine):
        finance_engine.add_income_source("Salary", amount=40000)
        for d in range(1, 4):
            _spend(finance_engine, 500, d)   # this week: 1500
        for d in range(8, 11):
            _spend(finance_engine, 3000, d)  # prev week: 9000

        from amy.engines.predictive_engine import PredictiveEngine
        fc = PredictiveEngine(None).forecast_finance(finance_engine)
        assert fc["trend"] == "down"
        assert fc["projected_next_week_spend"] == 0  # max(0, 1500-7500)

    def test_trend_up_when_spend_increasing(self, finance_engine):
        finance_engine.add_income_source("Salary", amount=40000)
        for d in range(1, 4):
            _spend(finance_engine, 3000, d)   # this week: 9000
        for d in range(8, 11):
            _spend(finance_engine, 1000, d)   # prev week: 3000

        from amy.engines.predictive_engine import PredictiveEngine
        fc = PredictiveEngine(None).forecast_finance(finance_engine)
        assert fc["trend"] == "up"
        assert fc["projected_next_week_spend"] > fc["this_week_spend"]

    def test_no_income_no_alert(self, finance_engine):
        """Without income, comfortable_weekly=0, alert stays False."""
        for d in range(1, 7):
            _spend(finance_engine, 50000, d)

        from amy.engines.predictive_engine import PredictiveEngine
        fc = PredictiveEngine(None).forecast_finance(finance_engine)
        assert fc["alert"] is False  # can't compare without income baseline

    def test_positive_transactions_not_counted_as_spend(self, finance_engine):
        finance_engine.add_income_source("Salary", amount=20000)
        # Income credit — should NOT count as spend
        finance_engine.add_transaction(20000, "Income", date=_date(2))
        _spend(finance_engine, 500, 3)

        from amy.engines.predictive_engine import PredictiveEngine
        fc = PredictiveEngine(None).forecast_finance(finance_engine)
        assert fc["this_week_spend"] == 500

    def test_forecast_structure(self, finance_engine):
        from amy.engines.predictive_engine import PredictiveEngine
        fc = PredictiveEngine(None).forecast_finance(finance_engine)
        required_keys = {"metric", "this_week_spend", "prev_week_spend",
                         "trend", "projected_next_week_spend",
                         "monthly_income", "comfortable_weekly",
                         "alert", "note", "confidence"}
        assert required_keys.issubset(fc.keys())

    def test_scheduler_includes_cashflow_in_payload(self, finance_engine, collab_db):
        """Digest payload includes cashflow_forecast key."""
        from amy.events.scheduler import generate_and_store
        from amy.events.store import EventStore
        generate_and_store(collab_db, finance_db_path=str(finance_engine.path))
        digest_events = EventStore(collab_db).recent("digest.generated", n=5)
        assert len(digest_events) > 0
        payload = digest_events[0]["payload"]
        assert "cashflow_forecast" in payload

    def test_cashflow_alert_creates_notification(self, finance_engine, collab_db):
        """When alert fires, a cashflow_alert notification is created in collab.db."""
        from amy.events.scheduler import generate_and_store
        from amy.notifications import NotificationStore
        finance_engine.add_income_source("Salary", amount=10000)
        # Overspend heavily this week vs last
        for d in range(1, 7):
            _spend(finance_engine, 5000, d)
        for d in range(8, 14):
            _spend(finance_engine, 100, d)

        generate_and_store(collab_db, finance_db_path=str(finance_engine.path))
        store = NotificationStore(collab_db)
        notifs = [n for n in store.list() if n["type"] == "cashflow_alert"]
        assert len(notifs) == 1
        assert notifs[0]["priority"] == "high"
