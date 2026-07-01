"""Tests for Automation 2: Pattern nudge notifications."""
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
def store(collab_db):
    from amy.notifications import NotificationStore
    return NotificationStore(collab_db)


@pytest.fixture()
def svc(store):
    from amy.notifications import NotificationService
    return NotificationService(store)


@pytest.fixture()
def finance_engine(tmp_path):
    from amy.finance.engine import FinanceEngine
    fe = FinanceEngine(str(tmp_path / "finance.db"))
    yield fe
    fe.close()


def _date(days_ago: int) -> str:
    return (_dt.date.today() - _dt.timedelta(days=days_ago)).isoformat()


def _add_spend(fe, category: str, amount: float, days_ago: int):
    fe.add_transaction(-amount, category, date=_date(days_ago))


class TestSpendingPatternNudges:
    def test_no_budget_no_nudge(self, svc, finance_engine):
        """Without any budgets set, no pattern nudge is generated."""
        for i in range(4):
            _add_spend(finance_engine, "Food & Dining", 600, i * 7 + 1)
        created = svc.evaluate_finance(finance_engine)
        types = [n["type"] for n in svc.store.list()]
        assert "spending_pattern" not in types

    def test_3_weeks_over_budget_creates_nudge(self, svc, finance_engine):
        """Over weekly limit in 3 of last 4 weeks triggers a pattern nudge."""
        finance_engine.set_budget("Food & Dining", 2000)  # 500/week limit
        # Overspend weeks 0, 1, 2 (600 > 500)
        for w in range(3):
            _add_spend(finance_engine, "Food & Dining", 600, w * 7 + 1)
        # Week 3: under budget
        _add_spend(finance_engine, "Food & Dining", 400, 21 + 1)

        created = svc.evaluate_finance(finance_engine)
        pattern_notifs = [n for n in svc.store.list()
                          if n["type"] == "spending_pattern"]
        assert len(pattern_notifs) == 1
        n = pattern_notifs[0]
        assert "Food & Dining" in n["title"]
        assert "3 of the last 4 weeks" in n["body"]
        assert n["priority"] == "normal"

    def test_only_2_weeks_over_no_nudge(self, svc, finance_engine):
        """2 of 4 weeks over budget does not trigger a pattern nudge."""
        finance_engine.set_budget("Transport", 1200)  # 300/week
        _add_spend(finance_engine, "Transport", 400, 1)   # week 0 — over
        _add_spend(finance_engine, "Transport", 400, 8)   # week 1 — over
        _add_spend(finance_engine, "Transport", 200, 15)  # week 2 — under
        _add_spend(finance_engine, "Transport", 200, 22)  # week 3 — under

        svc.evaluate_finance(finance_engine)
        pattern_notifs = [n for n in svc.store.list()
                          if n["type"] == "spending_pattern"]
        assert len(pattern_notifs) == 0

    def test_all_4_weeks_over_creates_nudge(self, svc, finance_engine):
        finance_engine.set_budget("Shopping", 2000)  # 500/week
        for w in range(4):
            _add_spend(finance_engine, "Shopping", 700, w * 7 + 2)

        svc.evaluate_finance(finance_engine)
        pattern_notifs = [n for n in svc.store.list()
                          if n["type"] == "spending_pattern"]
        assert len(pattern_notifs) == 1

    def test_pattern_nudge_deduped_within_24h(self, svc, finance_engine):
        """Second call within 24h doesn't create a second pattern notification."""
        finance_engine.set_budget("Food & Dining", 2000)
        for w in range(3):
            _add_spend(finance_engine, "Food & Dining", 600, w * 7 + 1)

        svc.evaluate_finance(finance_engine)
        count_first = len([n for n in svc.store.list()
                           if n["type"] == "spending_pattern"])
        svc.evaluate_finance(finance_engine)
        count_second = len([n for n in svc.store.list()
                            if n["type"] == "spending_pattern"])
        assert count_first == 1
        assert count_second == 1   # no duplicate

    def test_multiple_categories_each_get_nudge(self, svc, finance_engine):
        finance_engine.set_budget("Food & Dining", 2000)
        finance_engine.set_budget("Transport", 1200)
        for w in range(3):
            _add_spend(finance_engine, "Food & Dining", 600, w * 7 + 1)
            _add_spend(finance_engine, "Transport", 500, w * 7 + 2)

        svc.evaluate_finance(finance_engine)
        pattern_notifs = [n for n in svc.store.list()
                          if n["type"] == "spending_pattern"]
        categories = {n["related_entity"]["category"] for n in pattern_notifs}
        assert "Food & Dining" in categories
        assert "Transport" in categories

    def test_related_entity_contains_weeks_over(self, svc, finance_engine):
        finance_engine.set_budget("Entertainment", 1000)
        for w in range(4):
            _add_spend(finance_engine, "Entertainment", 400, w * 7 + 1)

        svc.evaluate_finance(finance_engine)
        n = next(n for n in svc.store.list() if n["type"] == "spending_pattern")
        assert n["related_entity"]["weeks_over"] == 4
        assert n["related_entity"]["category"] == "Entertainment"

    def test_pattern_nudge_body_contains_amounts(self, svc, finance_engine):
        finance_engine.set_budget("Shopping", 4000)  # 1000/week
        for w in range(3):
            _add_spend(finance_engine, "Shopping", 1500, w * 7 + 1)

        svc.evaluate_finance(finance_engine)
        n = next(n for n in svc.store.list() if n["type"] == "spending_pattern")
        assert "1,000" in n["body"] or "1000" in n["body"]  # weekly limit
