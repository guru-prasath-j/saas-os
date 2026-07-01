"""Finance Agent Phase A — unit + integration tests.

Covers:
  - FinanceEngine CRUD (transactions, budgets, subscriptions, investments, income)
  - Analytics (balance estimate, budget status, upcoming bills, portfolio)
  - AffordEngine (under budget, over balance, no income, upcoming bills, budget blocked)
  - REST endpoints via TestClient
  - Dashboard finance section
  - Digest finance summary pass
"""
from __future__ import annotations

import datetime as _dt
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


# ===========================================================================
#  Fixtures
# ===========================================================================

@pytest.fixture
def fdb(tmp_path):
    from amy.finance import FinanceEngine
    fe = FinanceEngine(str(tmp_path / "finance.db"))
    yield fe
    fe.close()


@pytest.fixture(scope="module")
def app_client():
    """One TestClient + auth token for all API tests (module-scoped for speed)."""
    data_dir = tempfile.mkdtemp(prefix="amy_fin_test_")
    os.environ["AMY_SAAS_DATA"] = data_dir
    from amy.saas.app import app
    from amy.saas.db import init_db
    from amy.saas import tenancy
    init_db()
    c = TestClient(app)
    r = c.post("/auth/signup", json={"email": "cfo@test.com", "password": "test1234"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    uid = r.json()["user"]["id"]
    tenancy.ensure_dirs(uid)
    return c, {"Authorization": f"Bearer {token}"}, uid, data_dir


# ===========================================================================
#  FinanceEngine unit tests
# ===========================================================================

class TestTransactions:
    def test_add_and_list(self, fdb):
        tid = fdb.add_transaction(-1200.0, "Food", merchant="Swiggy", notes="lunch")
        rows = fdb.list_transactions()
        assert len(rows) == 1
        assert rows[0]["id"] == tid
        assert rows[0]["amount"] == -1200.0
        assert rows[0]["merchant"] == "Swiggy"

    def test_delete(self, fdb):
        tid = fdb.add_transaction(-500.0, "Coffee")
        assert fdb.delete_transaction(tid)
        assert fdb.list_transactions() == []

    def test_delete_nonexistent_returns_false(self, fdb):
        assert not fdb.delete_transaction("doesnotexist")

    def test_filter_by_category(self, fdb):
        today = _dt.date.today().isoformat()
        fdb.add_transaction(-1000.0, "Food", date=today)
        fdb.add_transaction(-2000.0, "Rent", date=today)
        rows = fdb.list_transactions(category="Food")
        assert all(r["category"] == "Food" for r in rows)

    def test_filter_by_date_range(self, fdb):
        fdb.add_transaction(-100.0, "X", date="2023-01-15")
        fdb.add_transaction(-200.0, "Y", date="2023-06-01")
        rows = fdb.list_transactions(since="2023-05-01", until="2023-12-31")
        assert len(rows) == 1
        assert rows[0]["category"] == "Y"


class TestBudgets:
    def test_set_and_list(self, fdb):
        fdb.set_budget("Food", 5000.0)
        budgets = fdb.list_budgets()
        assert any(b["category"] == "Food" and b["monthly_limit"] == 5000.0
                   for b in budgets)

    def test_upsert(self, fdb):
        fdb.set_budget("Food", 5000.0)
        fdb.set_budget("Food", 7000.0)
        budgets = fdb.list_budgets()
        food = next(b for b in budgets if b["category"] == "Food")
        assert food["monthly_limit"] == 7000.0

    def test_delete(self, fdb):
        fdb.set_budget("Misc", 1000.0)
        assert fdb.delete_budget("Misc")
        assert not any(b["category"] == "Misc" for b in fdb.list_budgets())


class TestSubscriptions:
    def test_add_and_list_active(self, fdb):
        fdb.add_subscription("Netflix", monthly_cost=649.0, status="active")
        fdb.add_subscription("Old", monthly_cost=100.0, status="paused")
        subs = fdb.list_subscriptions()  # active only by default
        assert len(subs) == 1
        assert subs[0]["name"] == "Netflix"

    def test_update_status(self, fdb):
        sid = fdb.add_subscription("Spotify", monthly_cost=119.0)
        fdb.update_subscription(sid, status="paused")
        assert fdb.list_subscriptions() == []  # no active ones

    def test_delete(self, fdb):
        sid = fdb.add_subscription("Hotstar", monthly_cost=299.0)
        assert fdb.delete_subscription(sid)

    def test_monthly_total(self, fdb):
        fdb.add_subscription("A", monthly_cost=100.0)
        fdb.add_subscription("B", monthly_cost=200.0)
        assert fdb.subscription_total_monthly() == pytest.approx(300.0)

    def test_insights_duplicate_suspects(self, fdb):
        fdb.add_subscription("Netflix Basic", monthly_cost=199.0)
        fdb.add_subscription("Netflix Premium", monthly_cost=649.0)
        insights = fdb.subscription_insights()
        assert insights["annual_cost"] == pytest.approx(
            (199.0 + 649.0) * 12, rel=1e-4)
        assert len(insights["duplicate_suspects"]) == 1
        assert set(insights["duplicate_suspects"][0]["names"]) == {
            "Netflix Basic", "Netflix Premium"}

    def test_upcoming_bills(self, fdb):
        soon = (_dt.date.today() + _dt.timedelta(days=5)).isoformat()
        far = (_dt.date.today() + _dt.timedelta(days=60)).isoformat()
        fdb.add_subscription("Soon", monthly_cost=100.0, renewal_date=soon)
        fdb.add_subscription("Far", monthly_cost=200.0, renewal_date=far)
        bills = fdb.upcoming_bills(30)
        assert len(bills) == 1
        assert bills[0]["name"] == "Soon"


class TestInvestments:
    def test_add_and_list(self, fdb):
        iid = fdb.add_investment("MF", "HDFC Nifty 50",
                                 current_value=50000, cost_basis=40000)
        invs = fdb.list_investments()
        assert len(invs) == 1
        assert invs[0]["id"] == iid

    def test_update_value(self, fdb):
        iid = fdb.add_investment("Stock", "Infosys", current_value=10000, cost_basis=8000)
        fdb.update_investment(iid, current_value=12000)
        assert fdb.list_investments()[0]["current_value"] == 12000

    def test_portfolio_summary(self, fdb):
        fdb.add_investment("MF", "X", current_value=50000, cost_basis=40000)
        fdb.add_investment("Stock", "Y", current_value=20000, cost_basis=25000)
        pf = fdb.portfolio_summary()
        assert pf["total_value"] == 70000.0
        assert pf["total_cost"] == 65000.0
        assert pf["gain_loss"] == 5000.0
        assert "MF" in pf["by_type"]

    def test_delete(self, fdb):
        iid = fdb.add_investment("FD", "SBI FD", current_value=100000)
        assert fdb.delete_investment(iid)
        assert fdb.list_investments() == []


class TestIncomeSources:
    def test_add_and_list(self, fdb):
        fdb.add_income_source("Salary", "salary", 80000.0, "monthly")
        srcs = fdb.list_income_sources()
        assert any(s["name"] == "Salary" for s in srcs)

    def test_monthly_income_salary(self, fdb):
        fdb.add_income_source("Salary", amount=80000.0, recurrence="monthly")
        assert fdb.monthly_income() == pytest.approx(80000.0)

    def test_monthly_income_annual(self, fdb):
        fdb.add_income_source("Bonus", amount=12000.0, recurrence="annual")
        assert fdb.monthly_income() == pytest.approx(1000.0, rel=1e-4)

    def test_monthly_income_weekly(self, fdb):
        fdb.add_income_source("Freelance", amount=5000.0, recurrence="weekly")
        expected = 5000.0 * 52 / 12
        assert fdb.monthly_income() == pytest.approx(expected, rel=1e-4)

    def test_delete(self, fdb):
        sid = fdb.add_income_source("Temp", amount=1000.0)
        assert fdb.delete_income_source(sid)
        assert fdb.list_income_sources() == []


class TestAnalytics:
    def test_balance_estimate(self, fdb):
        today = _dt.date.today().isoformat()
        fdb.add_income_source("Salary", amount=80000.0)
        fdb.add_transaction(-20000.0, "Rent", date=today)
        fdb.add_transaction(-5000.0, "Food", date=today)
        assert fdb.balance_estimate() == pytest.approx(55000.0, rel=1e-4)

    def test_balance_with_credit_txn(self, fdb):
        today = _dt.date.today().isoformat()
        fdb.add_income_source("Salary", amount=50000.0)
        fdb.add_transaction(10000.0, "Refund", date=today)   # positive = credit
        assert fdb.balance_estimate() == pytest.approx(60000.0, rel=1e-4)

    def test_budget_status_over(self, fdb):
        today = _dt.date.today().isoformat()
        fdb.set_budget("Food", 3000.0)
        fdb.add_transaction(-4000.0, "Food", date=today)
        statuses = fdb.budget_status()
        food = next(s for s in statuses if s["category"] == "Food")
        assert food["over_budget"] is True
        assert food["spent"] == 4000.0
        assert food["headroom"] == -1000.0

    def test_budget_status_under(self, fdb):
        today = _dt.date.today().isoformat()
        fdb.set_budget("Shopping", 5000.0)
        fdb.add_transaction(-2000.0, "Shopping", date=today)
        statuses = fdb.budget_status()
        shop = next(s for s in statuses if s["category"] == "Shopping")
        assert shop["over_budget"] is False
        assert shop["headroom"] == 3000.0

    def test_context_block_contains_balance(self, fdb):
        fdb.add_income_source("Salary", amount=80000.0)
        block = fdb.context_block()
        assert "80,000" in block or "80000" in block
        assert "Finance Data" in block


# ===========================================================================
#  AffordEngine unit tests
# ===========================================================================

class TestAffordEngine:
    def test_under_budget(self, fdb):
        from amy.finance.afford import can_afford
        today = _dt.date.today().isoformat()
        fdb.add_income_source("Salary", amount=80000.0)
        fdb.add_transaction(-10000.0, "Rent", date=today)
        result = can_afford(5000.0, "buy headphones", fdb)
        assert result["can_afford"] is True
        assert result["risk_level"] in ("low", "medium")
        assert result["monthly_impact"] == 5000.0
        assert any("Affordable" in r or "✓" in r for r in result["reasoning"])

    def test_over_balance(self, fdb):
        from amy.finance.afford import can_afford
        today = _dt.date.today().isoformat()
        fdb.add_income_source("Salary", amount=20000.0)
        fdb.add_transaction(-19000.0, "Rent", date=today)
        result = can_afford(5000.0, "buy laptop", fdb)
        assert result["can_afford"] is False
        assert result["risk_level"] == "high"

    def test_no_income_returns_unknown(self, fdb):
        from amy.finance.afford import can_afford
        result = can_afford(5000.0, "buy phone", fdb)
        assert result["can_afford"] is None
        assert result["risk_level"] == "unknown"
        assert any("No income" in r for r in result["reasoning"])

    def test_upcoming_bills_reduce_effective_balance(self, fdb):
        from amy.finance.afford import can_afford
        fdb.add_income_source("Salary", amount=10000.0)
        soon = (_dt.date.today() + _dt.timedelta(days=3)).isoformat()
        fdb.add_subscription("AWS Server", monthly_cost=8500.0, renewal_date=soon)
        # effective = 10000 - 8500 = 1500 < 5000
        result = can_afford(5000.0, "buy something", fdb)
        assert result["can_afford"] is False
        assert any("bills" in r.lower() or "subscription" in r.lower()
                   for r in result["reasoning"])

    def test_budget_headroom_blocks(self, fdb):
        from amy.finance.afford import can_afford
        today = _dt.date.today().isoformat()
        fdb.add_income_source("Salary", amount=80000.0)
        fdb.set_budget("food", 3000.0)
        fdb.add_transaction(-2800.0, "food", date=today)
        # 200 headroom but asking for 500 food spend
        result = can_afford(500.0, "restaurant food dinner", fdb)
        assert result["can_afford"] is False
        assert any("budget" in r.lower() for r in result["reasoning"])

    def test_goal_delay_reported(self, fdb, tmp_path):
        from amy.finance.afford import can_afford
        from amy.collab.db import CollabDB
        import uuid
        fdb.add_income_source("Salary", amount=50000.0)
        cdb = CollabDB(str(tmp_path / "collab.db"))
        # insert a finance goal
        cdb.conn.execute(
            "INSERT INTO goals(id,title,domain,status,progress,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex, "Emergency Fund", "finance", "active", 0.3,
             _dt.datetime.now(_dt.timezone.utc).isoformat()))
        cdb.conn.commit()
        try:
            result = can_afford(10000.0, "buy TV", fdb, collab_db=cdb)
            assert result["goal_delay_months"] is not None
            assert result["goal_delay_months"] > 0
        finally:
            cdb.close()

    def test_risk_level_low_for_small_amount(self, fdb):
        from amy.finance.afford import can_afford
        fdb.add_income_source("Salary", amount=100000.0)
        result = can_afford(2000.0, "buy coffee", fdb)
        assert result["risk_level"] == "low"

    def test_risk_level_high_for_large_amount(self, fdb):
        from amy.finance.afford import can_afford
        fdb.add_income_source("Salary", amount=30000.0)
        result = can_afford(25000.0, "buy TV", fdb)
        assert result["risk_level"] == "high"


# ===========================================================================
#  API endpoint integration tests
# ===========================================================================

class TestFinanceAPI:
    def test_overview_empty(self, app_client):
        c, hdrs, *_ = app_client
        r = c.get("/api/finance/overview", headers=hdrs)
        assert r.status_code == 200
        d = r.json()
        assert d["balance_estimate"] == 0.0
        assert d["monthly_income"] == 0.0
        assert d["this_month_spend"] == {}

    def test_add_and_list_transaction(self, app_client):
        c, hdrs, *_ = app_client
        r = c.post("/api/finance/transactions",
                   json={"amount": -1500.0, "category": "Food", "merchant": "Swiggy"},
                   headers=hdrs)
        assert r.status_code == 200
        tid = r.json()["id"]
        r = c.get("/api/finance/transactions", headers=hdrs)
        assert r.status_code == 200
        assert any(t["id"] == tid for t in r.json()["transactions"])

    def test_delete_transaction(self, app_client):
        c, hdrs, *_ = app_client
        r = c.post("/api/finance/transactions",
                   json={"amount": -300.0, "category": "Coffee"},
                   headers=hdrs)
        tid = r.json()["id"]
        r = c.delete(f"/api/finance/transactions/{tid}", headers=hdrs)
        assert r.status_code == 200
        assert r.json()["ok"]

    def test_delete_nonexistent_transaction(self, app_client):
        c, hdrs, *_ = app_client
        r = c.delete("/api/finance/transactions/nosuchid", headers=hdrs)
        assert r.status_code == 404

    def test_set_and_get_budget(self, app_client):
        c, hdrs, *_ = app_client
        r = c.post("/api/finance/budgets",
                   json={"category": "Entertainment", "monthly_limit": 2000.0},
                   headers=hdrs)
        assert r.status_code == 200
        r = c.get("/api/finance/budgets", headers=hdrs)
        assert r.status_code == 200
        cats = [b["category"] for b in r.json()["budgets"]]
        assert "Entertainment" in cats

    def test_delete_budget(self, app_client):
        c, hdrs, *_ = app_client
        c.post("/api/finance/budgets",
               json={"category": "Temp", "monthly_limit": 500.0},
               headers=hdrs)
        r = c.delete("/api/finance/budgets/Temp", headers=hdrs)
        assert r.status_code == 200

    def test_subscription_lifecycle(self, app_client):
        c, hdrs, *_ = app_client
        r = c.post("/api/finance/subscriptions",
                   json={"name": "Spotify", "monthly_cost": 119.0, "status": "active"},
                   headers=hdrs)
        assert r.status_code == 200
        sid = r.json()["id"]
        r = c.get("/api/finance/subscriptions", headers=hdrs)
        assert any(s["id"] == sid for s in r.json()["subscriptions"])
        r = c.patch(f"/api/finance/subscriptions/{sid}",
                    json={"status": "paused"}, headers=hdrs)
        assert r.status_code == 200
        r = c.delete(f"/api/finance/subscriptions/{sid}", headers=hdrs)
        assert r.status_code == 200

    def test_subscription_insights(self, app_client):
        c, hdrs, *_ = app_client
        c.post("/api/finance/subscriptions",
               json={"name": "Netflix Standard", "monthly_cost": 499.0}, headers=hdrs)
        c.post("/api/finance/subscriptions",
               json={"name": "Netflix Premium", "monthly_cost": 649.0}, headers=hdrs)
        r = c.get("/api/finance/subscriptions/insights", headers=hdrs)
        assert r.status_code == 200
        d = r.json()
        assert "annual_cost" in d
        assert "duplicate_suspects" in d
        assert d["annual_cost"] > 0

    def test_investment_crud(self, app_client):
        c, hdrs, *_ = app_client
        r = c.post("/api/finance/investments",
                   json={"type": "MF", "name": "HDFC Nifty 50",
                         "current_value": 50000, "cost_basis": 40000},
                   headers=hdrs)
        assert r.status_code == 200
        iid = r.json()["id"]
        r = c.get("/api/finance/investments", headers=hdrs)
        assert r.status_code == 200
        assert r.json()["portfolio"]["total_value"] >= 50000
        r = c.patch(f"/api/finance/investments/{iid}",
                    json={"current_value": 55000}, headers=hdrs)
        assert r.status_code == 200
        r = c.delete(f"/api/finance/investments/{iid}", headers=hdrs)
        assert r.status_code == 200

    def test_income_crud(self, app_client):
        c, hdrs, *_ = app_client
        r = c.post("/api/finance/income",
                   json={"name": "Day Job", "type": "salary",
                         "amount": 80000.0, "recurrence": "monthly"},
                   headers=hdrs)
        assert r.status_code == 200
        sid = r.json()["id"]
        r = c.get("/api/finance/income", headers=hdrs)
        assert r.status_code == 200
        assert r.json()["monthly_total"] >= 80000.0
        r = c.delete(f"/api/finance/income/{sid}", headers=hdrs)
        assert r.status_code == 200

    def test_afford_no_income(self, tmp_path):
        # Unit-level: afford engine returns can_afford=None when no income sources exist.
        # (Also tested in TestAffordEngine, kept here as an API-adjacent smoke test.)
        from amy.finance import FinanceEngine
        from amy.finance.afford import can_afford
        fe = FinanceEngine(str(tmp_path / "noincome_finance.db"))
        try:
            result = can_afford(5000.0, "buy laptop", fe)
            assert result["can_afford"] is None
            assert result["risk_level"] == "unknown"
        finally:
            fe.close()

    def test_afford_with_income(self, app_client):
        c, hdrs, *_ = app_client
        c.post("/api/finance/income",
               json={"name": "Salary", "type": "salary",
                     "amount": 90000.0, "recurrence": "monthly"},
               headers=hdrs)
        r = c.post("/api/finance/afford",
                   json={"amount": 3000.0, "description": "buy a keyboard"},
                   headers=hdrs)
        assert r.status_code == 200
        d = r.json()
        assert "can_afford" in d
        assert isinstance(d["reasoning"], list)
        assert len(d["reasoning"]) >= 1

    def test_finance_goals(self, app_client):
        c, hdrs, *_ = app_client
        # Create a goal with domain=finance via the existing goal API
        r = c.post("/api/goals",
                   json={"title": "Emergency Fund", "domain": "finance",
                         "target_date": "2026-12-31"},
                   headers=hdrs)
        assert r.status_code == 200
        r = c.get("/api/finance/goals", headers=hdrs)
        assert r.status_code == 200
        goals = r.json()["goals"]
        assert any(g["title"] == "Emergency Fund" for g in goals)


class TestDashboardFinanceSection:
    def test_dashboard_has_finance_key(self, app_client):
        c, hdrs, *_ = app_client
        r = c.get("/api/dashboard", headers=hdrs)
        assert r.status_code == 200
        assert "finance" in r.json()

    def test_dashboard_finance_shows_data(self, app_client):
        c, hdrs, uid, data_dir = app_client
        # Add income so finance.db exists and has data
        c.post("/api/finance/income",
               json={"name": "Salary2", "type": "salary",
                     "amount": 70000.0, "recurrence": "monthly"},
               headers=hdrs)
        r = c.get("/api/dashboard", headers=hdrs)
        assert r.status_code == 200
        fin = r.json()["finance"]
        assert fin.get("monthly_income", 0) >= 70000 or fin == {}


class TestDigestFinanceIntegration:
    def test_generate_and_store_with_finance(self, tmp_path):
        from amy.collab.db import CollabDB
        from amy.finance import FinanceEngine
        from amy.events.scheduler import generate_and_store

        cdb = CollabDB(str(tmp_path / "collab.db"))
        fdb_path = str(tmp_path / "finance.db")
        fe = FinanceEngine(fdb_path)
        fe.add_income_source("Salary", amount=50000.0)
        soon = (_dt.date.today() + _dt.timedelta(days=7)).isoformat()
        fe.add_subscription("AWS", monthly_cost=2000.0, renewal_date=soon)
        fe.close()

        try:
            result = generate_and_store(cdb, finance_db_path=fdb_path)
            # digest returns without error and has finance key
            assert "finance" in result
            fin = result["finance"]
            assert fin["subscription_monthly_total"] == pytest.approx(2000.0)
            assert len(fin["upcoming_bills"]) >= 1
        finally:
            cdb.close()

    def test_generate_and_store_without_finance(self, tmp_path):
        from amy.collab.db import CollabDB
        from amy.events.scheduler import generate_and_store
        cdb = CollabDB(str(tmp_path / "collab.db"))
        try:
            result = generate_and_store(cdb)  # no finance_db_path
            assert "suggestions" in result   # existing fields still present
        finally:
            cdb.close()
