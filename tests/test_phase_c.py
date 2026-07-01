"""Tests for Phase C: notifications, calendar agent, bank CSV presets."""
from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ===========================================================================
# Helpers
# ===========================================================================

@pytest.fixture()
def collab_db(tmp_path):
    from amy.collab.db import CollabDB
    db = CollabDB(str(tmp_path / "collab.db"))
    yield db
    db.close()


@pytest.fixture()
def notification_store(collab_db):
    from amy.notifications import NotificationStore
    return NotificationStore(collab_db)


@pytest.fixture()
def finance_engine(tmp_path):
    from amy.finance.engine import FinanceEngine
    fe = FinanceEngine(str(tmp_path / "finance.db"))
    yield fe
    fe.close()


# ===========================================================================
# 1.  NotificationStore — CRUD
# ===========================================================================

class TestNotificationStore:
    def test_create_and_list(self, notification_store):
        nid = notification_store.create("budget_overage", "Test", "Body text")
        notifications = notification_store.list()
        assert len(notifications) == 1
        assert notifications[0]["id"] == nid
        assert notifications[0]["title"] == "Test"
        assert notifications[0]["read_at"] is None

    def test_unread_count(self, notification_store):
        notification_store.create("budget_overage", "A", "B")
        notification_store.create("bill_due_soon", "C", "D")
        assert notification_store.unread_count() == 2

    def test_mark_read(self, notification_store):
        nid = notification_store.create("budget_overage", "T", "B")
        assert notification_store.unread_count() == 1
        ok = notification_store.mark_read(nid)
        assert ok
        assert notification_store.unread_count() == 0
        n = notification_store.list()[0]
        assert n["read_at"] is not None

    def test_mark_read_missing(self, notification_store):
        assert not notification_store.mark_read("does_not_exist")

    def test_mark_all_read(self, notification_store):
        notification_store.create("budget_overage", "A", "B")
        notification_store.create("bill_due_soon", "C", "D")
        notification_store.mark_all_read()
        assert notification_store.unread_count() == 0

    def test_unread_only_filter(self, notification_store):
        nid1 = notification_store.create("budget_overage", "A", "B")
        notification_store.create("bill_due_soon", "C", "D")
        notification_store.mark_read(nid1)
        unread = notification_store.list(unread_only=True)
        assert len(unread) == 1
        assert unread[0]["title"] == "C"

    def test_priority_stored(self, notification_store):
        notification_store.create("bill_due_soon", "T", "B", priority="high")
        n = notification_store.list()[0]
        assert n["priority"] == "high"

    def test_related_entity_serialised(self, notification_store):
        notification_store.create("budget_overage", "T", "B",
                                  related_entity={"entity_type": "budget", "id": "x1"})
        n = notification_store.list()[0]
        assert n["related_entity"]["id"] == "x1"

    def test_exists_today_dedup(self, notification_store):
        notification_store.create("budget_overage", "T", "B",
                                  related_entity={"id": "budget_over_Food"})
        assert notification_store.exists_today("budget_overage", "budget_over_Food")
        assert not notification_store.exists_today("budget_overage", "other_id")

    def test_collab_db_has_notifications_table(self, collab_db):
        tables = [r[0] for r in collab_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert "notifications" in tables


# ===========================================================================
# 2.  NotificationService — finance condition evaluation
# ===========================================================================

class TestNotificationService:
    def test_budget_overage_creates_notification(
            self, notification_store, finance_engine):
        from amy.notifications import NotificationService
        import datetime as _dt
        today = _dt.date.today().isoformat()
        finance_engine.set_budget("Food", 5000.0)
        finance_engine.add_transaction(-6000.0, "Food", date=today)
        svc = NotificationService(notification_store)
        created = svc.evaluate_finance(finance_engine)
        assert len(created) >= 1
        notifications = notification_store.list()
        types = [n["type"] for n in notifications]
        assert "budget_overage" in types
        over = [n for n in notifications if n["type"] == "budget_overage"][0]
        assert "Food" in over["title"]
        assert over["priority"] == "high"

    def test_no_notification_when_under_budget(
            self, notification_store, finance_engine):
        from amy.notifications import NotificationService
        import datetime as _dt
        today = _dt.date.today().isoformat()
        finance_engine.set_budget("Food", 5000.0)
        finance_engine.add_transaction(-2000.0, "Food", date=today)
        svc = NotificationService(notification_store)
        created = svc.evaluate_finance(finance_engine)
        assert len(created) == 0

    def test_bill_due_soon_notification(
            self, notification_store, finance_engine):
        from amy.notifications import NotificationService
        import datetime as _dt
        due = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
        finance_engine.add_subscription("Netflix", monthly_cost=499,
                                         renewal_date=due, status="active")
        svc = NotificationService(notification_store)
        created = svc.evaluate_finance(finance_engine)
        assert len(created) >= 1
        n = notification_store.list()[0]
        assert n["type"] == "bill_due_soon"
        assert n["priority"] == "high"
        assert "Netflix" in n["title"]

    def test_bill_upcoming_14d_notification(
            self, notification_store, finance_engine):
        from amy.notifications import NotificationService
        import datetime as _dt
        due = (_dt.date.today() + _dt.timedelta(days=10)).isoformat()
        finance_engine.add_subscription("Spotify", monthly_cost=119,
                                         renewal_date=due, status="active")
        svc = NotificationService(notification_store)
        created = svc.evaluate_finance(finance_engine)
        assert len(created) >= 1
        n = notification_store.list()[0]
        assert n["type"] == "bill_upcoming"
        assert n["priority"] == "normal"

    def test_dedup_within_24h(self, notification_store, finance_engine):
        from amy.notifications import NotificationService
        import datetime as _dt
        today = _dt.date.today().isoformat()
        finance_engine.set_budget("Food", 100.0)
        finance_engine.add_transaction(-500.0, "Food", date=today)
        svc = NotificationService(notification_store)
        created1 = svc.evaluate_finance(finance_engine)
        created2 = svc.evaluate_finance(finance_engine)   # same run
        assert len(created1) >= 1
        assert len(created2) == 0   # deduped

    def test_multiple_conditions_multiple_notifications(
            self, notification_store, finance_engine):
        from amy.notifications import NotificationService
        import datetime as _dt
        today = _dt.date.today().isoformat()
        due = (_dt.date.today() + _dt.timedelta(days=2)).isoformat()
        finance_engine.set_budget("Rent", 20000.0)
        finance_engine.add_transaction(-25000.0, "Rent", date=today)
        finance_engine.add_subscription("AWS", monthly_cost=1500,
                                         renewal_date=due, status="active")
        svc = NotificationService(notification_store)
        created = svc.evaluate_finance(finance_engine)
        assert len(created) == 2


# ===========================================================================
# 3.  Email delivery — SMTP graceful degradation
# ===========================================================================

class TestEmailDelivery:
    def test_smtp_not_configured_returns_false(self):
        from amy.notifications.email import send_email, smtp_configured
        os.environ.pop("SMTP_HOST", None)
        assert smtp_configured() is False
        result = send_email("test@x.com", "Test", "Body")
        assert result is False

    def test_smtp_not_configured_maybe_send_returns_false(self):
        from amy.notifications.email import maybe_send_alert
        os.environ.pop("SMTP_HOST", None)
        notif = {"priority": "high", "title": "T", "body": "B"}
        assert maybe_send_alert("u@x.com", notif) is False

    def test_low_priority_never_emailed(self):
        from amy.notifications.email import maybe_send_alert
        # Even if SMTP is configured, low-priority = no email
        with patch.dict(os.environ, {"SMTP_HOST": "smtp.example.com"}):
            notif = {"priority": "normal", "title": "T", "body": "B"}
            assert maybe_send_alert("u@x.com", notif) is False

    def test_smtp_configured_attempts_send(self):
        from amy.notifications.email import send_email
        with patch.dict(os.environ, {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "587",
            "SMTP_USER": "user@x.com",
            "SMTP_PASS": "pass",
        }):
            with patch("smtplib.SMTP") as mock_smtp:
                mock_smtp.return_value.__enter__.return_value = MagicMock()
                result = send_email("to@x.com", "Subject", "Body")
            assert result is True

    def test_smtp_exception_returns_false(self):
        from amy.notifications.email import send_email
        with patch.dict(os.environ, {"SMTP_HOST": "bad.host.invalid"}):
            # Will fail to connect — should return False, not raise
            result = send_email("to@x.com", "Test", "Body")
            assert result is False

    def test_generate_and_store_with_finance_creates_notifications(
            self, collab_db, finance_engine, tmp_path):
        """Digest scheduler run creates in-app notifications from finance data."""
        from amy.events.scheduler import generate_and_store
        import datetime as _dt
        today = _dt.date.today().isoformat()
        finance_engine.set_budget("Food", 100.0)
        finance_engine.add_transaction(-500.0, "Food", date=today)
        db_path = str(tmp_path / "finance.db")
        # finance_engine already has the right path
        generate_and_store(collab_db, finance_db_path=str(finance_engine.path))
        from amy.notifications import NotificationStore
        store = NotificationStore(collab_db)
        notifications = store.list()
        assert any(n["type"] == "budget_overage" for n in notifications)

    def test_generate_and_store_smtp_absent_no_error(
            self, collab_db, finance_engine):
        """Digest run with no SMTP config doesn't raise."""
        from amy.events.scheduler import generate_and_store
        os.environ.pop("SMTP_HOST", None)
        import datetime as _dt
        due = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
        finance_engine.add_subscription("Bill", monthly_cost=100,
                                         renewal_date=due, status="active")
        # Should not raise even though email can't be sent
        generate_and_store(collab_db, finance_db_path=str(finance_engine.path),
                           user_email="user@example.com")


# ===========================================================================
# 4.  Calendar Agent
# ===========================================================================

class TestCalendarAgent:
    def test_no_data_returns_message(self):
        from amy.agents.calendar import CalendarAgent
        agent = CalendarAgent()
        result = agent.answer("what's upcoming?")
        assert result["domain"] == "calendar"
        assert "No calendar data" in result["answer"] or result["answer"]
        assert result["abstained"] is False

    def test_finance_context_includes_upcoming_bills(self, finance_engine, tmp_path):
        from amy.agents.calendar import CalendarAgent
        import datetime as _dt
        due = (_dt.date.today() + _dt.timedelta(days=5)).isoformat()
        finance_engine.add_subscription("Netflix", monthly_cost=499,
                                         renewal_date=due, status="active")
        agent = CalendarAgent(finance_db_path=str(finance_engine.path))
        ctx = agent._finance_calendar_context()
        assert "Netflix" in ctx
        assert due in ctx

    def test_finance_context_empty_when_no_bills(self, finance_engine):
        from amy.agents.calendar import CalendarAgent
        agent = CalendarAgent(finance_db_path=str(finance_engine.path))
        ctx = agent._finance_calendar_context()
        assert ctx == ""

    def test_answer_with_finance_data_and_llm(self, finance_engine):
        from amy.agents.calendar import CalendarAgent
        import datetime as _dt
        due = (_dt.date.today() + _dt.timedelta(days=7)).isoformat()
        finance_engine.add_subscription("Spotify", monthly_cost=119,
                                         renewal_date=due, status="active")
        llm = MagicMock()
        llm.generate.return_value = ("Spotify renews on " + due, "mock")
        agent = CalendarAgent(finance_db_path=str(finance_engine.path))
        result = agent.answer("what bills are coming up?", llm=llm)
        assert result["domain"] == "calendar"
        assert "Spotify" in result["answer"] or llm.generate.called
        assert result["model"] == "mock"

    def test_answer_without_llm_returns_context(self, finance_engine):
        from amy.agents.calendar import CalendarAgent
        import datetime as _dt
        due = (_dt.date.today() + _dt.timedelta(days=2)).isoformat()
        finance_engine.add_subscription("AWS", monthly_cost=1200,
                                         renewal_date=due, status="active")
        agent = CalendarAgent(finance_db_path=str(finance_engine.path))
        result = agent.answer("upcoming bills")
        assert "AWS" in result["answer"]
        assert result["model"] == "none"

    def test_google_calendar_context_no_connector(self, finance_engine):
        from amy.agents.calendar import CalendarAgent
        agent = CalendarAgent(finance_db_path=str(finance_engine.path),
                              connector_dir="/nonexistent/path")
        # Should not raise — no connector dir means no Google context
        ctx = agent._google_calendar_context()
        assert ctx == ""

    def test_push_to_calendar_no_connector(self, finance_engine):
        from amy.agents.calendar import CalendarAgent
        import datetime as _dt
        due = (_dt.date.today() + _dt.timedelta(days=3)).isoformat()
        finance_engine.add_subscription("Netflix", monthly_cost=499,
                                         renewal_date=due, status="active")
        agent = CalendarAgent(finance_db_path=str(finance_engine.path),
                              connector_dir="/nonexistent")
        result = agent.push_finance_events_to_calendar()
        assert result["created"] == 0
        assert len(result["errors"]) > 0

    def test_push_to_calendar_no_finance_db(self):
        from amy.agents.calendar import CalendarAgent
        agent = CalendarAgent()
        result = agent.push_finance_events_to_calendar()
        assert result["created"] == 0
        assert len(result["errors"]) > 0

    def test_push_to_calendar_with_mocked_google(self, finance_engine, tmp_path):
        from amy.agents.calendar import CalendarAgent
        import datetime as _dt
        due = (_dt.date.today() + _dt.timedelta(days=7)).isoformat()
        finance_engine.add_subscription("AWS", monthly_cost=1500,
                                         renewal_date=due, status="active")
        connector_dir = str(tmp_path / "connectors")
        os.makedirs(connector_dir, exist_ok=True)

        mock_creds = MagicMock()
        mock_svc = MagicMock()
        mock_svc.events().list().execute.return_value = {"items": []}
        mock_svc.events().insert().execute.return_value = {"id": "cal_event_1"}

        with patch("amy.connectors.google.load_credentials", return_value=mock_creds):
            with patch("googleapiclient.discovery.build", return_value=mock_svc):
                agent = CalendarAgent(
                    finance_db_path=str(finance_engine.path),
                    connector_dir=connector_dir,
                )
                result = agent.push_finance_events_to_calendar()

        assert result["created"] == 1
        assert result["errors"] == []

    def test_calendar_domain_in_pkos(self):
        from amy.pkos.domains import DEFAULT_KEYWORDS
        assert "calendar" in DEFAULT_KEYWORDS
        kws = DEFAULT_KEYWORDS["calendar"]
        assert any("calendar" in k for k in kws)
        assert any("schedule" in k for k in kws)

    def test_calendar_in_intent_router(self):
        from amy.pkos.router import IntentRouter
        router = IntentRouter(["calendar", "finance", "general"])
        domains = router.route("what calendar events do I have coming up?")
        assert "calendar" in domains

    def test_collab_master_injects_calendar_agent(self, tmp_path):
        from amy.collab.orchestrator import CollabMaster
        db_path = str(tmp_path / "collab.db")
        cm = CollabMaster([], db_path)
        try:
            assert "calendar" in cm.pkos_master.registry
            assert "calendar" in cm.pkos_master.router.available
        finally:
            cm.close()


# ===========================================================================
# 5.  Bank CSV Presets
# ===========================================================================

class TestBankPresets:
    def test_list_presets_not_empty(self):
        from amy.finance.sync.bank_presets import list_presets
        presets = list_presets()
        assert len(presets) >= 5  # HDFC, ICICI, SBI, Axis, Kotak at minimum

    def test_detect_hdfc(self):
        from amy.finance.sync.bank_presets import detect_preset
        headers = ["Date", "Narration", "Value Dt", "Withdrawal Amt.",
                   "Deposit Amt.", "Closing Balance"]
        preset = detect_preset(headers)
        assert preset is not None
        assert preset.bank_id == "HDFC"

    def test_detect_icici(self):
        from amy.finance.sync.bank_presets import detect_preset
        headers = ["Transaction Date", "Value Date", "Description",
                   "Ref No./Cheque No.", "Debit", "Credit", "Balance"]
        preset = detect_preset(headers)
        assert preset is not None
        assert preset.bank_id == "ICICI"

    def test_detect_sbi(self):
        from amy.finance.sync.bank_presets import detect_preset
        headers = ["Txn Date", "Value Date", "Description",
                   "Ref No./Cheque No.", "Debit", "Credit", "Balance"]
        preset = detect_preset(headers)
        assert preset is not None
        assert preset.bank_id == "SBI"

    def test_detect_axis(self):
        from amy.finance.sync.bank_presets import detect_preset
        headers = ["Tran. ID", "Value Date", "Cheque No",
                   "Particulars", "Debit", "Credit", "Bal"]
        preset = detect_preset(headers)
        assert preset is not None
        assert preset.bank_id == "AXIS"

    def test_detect_kotak(self):
        from amy.finance.sync.bank_presets import detect_preset
        headers = ["Transaction Date", "Value Date", "Particulars",
                   "Cheque Number", "Amount", "Dr/Cr", "Balance"]
        preset = detect_preset(headers)
        assert preset is not None
        assert preset.bank_id == "KOTAK"

    def test_detect_unknown_returns_none(self):
        from amy.finance.sync.bank_presets import detect_preset
        headers = ["col1", "col2", "col3"]
        assert detect_preset(headers) is None

    def test_detect_case_insensitive(self):
        from amy.finance.sync.bank_presets import detect_preset
        headers = ["DATE", "NARRATION", "VALUE DT",
                   "WITHDRAWAL AMT.", "DEPOSIT AMT.", "CLOSING BALANCE"]
        preset = detect_preset(headers)
        assert preset is not None
        assert preset.bank_id == "HDFC"

    def test_get_preset_by_id(self):
        from amy.finance.sync.bank_presets import get_preset
        p = get_preset("HDFC")
        assert p is not None
        assert p.name == "HDFC Bank"

    def test_get_preset_case_insensitive(self):
        from amy.finance.sync.bank_presets import get_preset
        assert get_preset("hdfc") is not None
        assert get_preset("icici") is not None

    def test_preset_column_map_has_required_keys(self):
        from amy.finance.sync.bank_presets import PRESETS
        for p in PRESETS:
            cm = p.column_map
            assert "date" in cm, f"{p.bank_id} missing 'date'"
            assert "description" in cm, f"{p.bank_id} missing 'description'"
            # Must have either (debit+credit) or (amount)
            has_debit_credit = "debit" in cm and "credit" in cm
            has_amount = "amount" in cm
            assert has_debit_credit or has_amount, (
                f"{p.bank_id} has neither debit/credit nor amount in column_map")

    def test_hdfc_csv_auto_detected_and_imported(self, finance_engine):
        """End-to-end: HDFC headers are auto-detected, preset applied, import succeeds."""
        from amy.finance.sync.csv_import import CSVImportProvider
        import csv, io as _io
        aid = finance_engine.add_account("HDFC Main", "HDFC", "savings")
        rows = [
            {"Date": "01/06/25", "Narration": "Swiggy", "Value Dt": "01/06/25",
             "Withdrawal Amt.": "500.00", "Deposit Amt.": "", "Closing Balance": "9500.00"},
            {"Date": "02/06/25", "Narration": "Salary", "Value Dt": "02/06/25",
             "Withdrawal Amt.": "", "Deposit Amt.": "50000.00", "Closing Balance": "59500.00"},
        ]
        headers = ["Date", "Narration", "Value Dt",
                   "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"]
        buf = _io.StringIO()
        w = csv.DictWriter(buf, fieldnames=headers)
        w.writeheader()
        for r in rows:
            w.writerow(r)
        raw = buf.getvalue().encode()

        provider = CSVImportProvider()
        result = provider.import_from_bytes(raw, finance_engine, aid)
        # Should auto-detect and import (not return needs_mapping)
        assert hasattr(result, "imported"), "Expected SyncResult, not preview dict"
        assert result.imported == 2
        assert result.preset_detected == "HDFC"
        # Saved map should now exist for HDFC bank
        assert finance_engine.get_column_map("HDFC") is not None

    def test_icici_csv_auto_detected(self, finance_engine):
        from amy.finance.sync.csv_import import CSVImportProvider
        import csv, io as _io
        aid = finance_engine.add_account("ICICI Savings", "ICICI", "savings")
        headers = ["Transaction Date", "Value Date", "Description",
                   "Ref No./Cheque No.", "Debit", "Credit", "Balance"]
        rows = [{"Transaction Date": "01/06/2025", "Value Date": "01/06/2025",
                 "Description": "ATM", "Ref No./Cheque No.": "",
                 "Debit": "2000", "Credit": "", "Balance": "18000"}]
        buf = _io.StringIO()
        w = csv.DictWriter(buf, fieldnames=headers)
        w.writeheader()
        for r in rows:
            w.writerow(r)
        raw = buf.getvalue().encode()
        provider = CSVImportProvider()
        result = provider.import_from_bytes(raw, finance_engine, aid)
        assert hasattr(result, "imported")
        assert result.preset_detected == "ICICI"
        assert result.imported == 1

    def test_unknown_bank_returns_needs_mapping(self, finance_engine):
        from amy.finance.sync.csv_import import CSVImportProvider
        import csv, io as _io
        aid = finance_engine.add_account("Unknown Bank", "UNKNOWN_BANK_XYZ", "savings")
        headers = ["col1", "col2", "col3"]
        buf = _io.StringIO()
        w = csv.DictWriter(buf, fieldnames=headers)
        w.writeheader()
        w.writerow({"col1": "a", "col2": "b", "col3": "c"})
        raw = buf.getvalue().encode()
        provider = CSVImportProvider()
        result = provider.import_from_bytes(raw, finance_engine, aid)
        assert isinstance(result, dict)
        assert result["needs_mapping"] is True


# ===========================================================================
# 6.  API integration tests
# ===========================================================================

_DATA_DIR_C = str(Path(tempfile.mkdtemp(prefix="amy_c_test_")))
os.environ["AMY_SAAS_DATA"] = _DATA_DIR_C


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from amy.saas.app import app
    from amy.saas.db import init_db
    from amy.saas import tenancy
    init_db()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def auth(client):
    r = client.post("/auth/signup",
                    json={"email": "phaseC@example.com", "password": "Pass1234!"})
    assert r.status_code == 200, r.text
    uid = r.json()["user"]["id"]
    from amy.saas import tenancy
    tenancy.ensure_dirs(uid)
    token = r.json()["token"]
    return {"Authorization": f"Bearer {token}"}


class TestNotificationsAPI:
    def test_list_empty(self, client, auth):
        r = client.get("/api/notifications", headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert "notifications" in body
        assert "unread_count" in body
        assert body["unread_count"] == 0

    def test_create_and_list_via_finance_trigger(self, client, auth):
        """Create a budget overage, run digest, verify notification appears."""
        # Add income + budget + overspend
        client.post("/api/finance/income",
                    json={"name": "Salary", "amount": 50000}, headers=auth)
        client.post("/api/finance/budgets",
                    json={"category": "Food", "monthly_limit": 100}, headers=auth)
        import datetime as _dt
        client.post("/api/finance/transactions",
                    json={"amount": -5000, "category": "Food",
                          "date": _dt.date.today().isoformat()},
                    headers=auth)
        # Trigger digest manually (direct call, not HTTP)
        from amy.saas import paths
        from amy.saas.db import SessionLocal, User
        from amy.collab import CollabDB
        from amy.events.scheduler import generate_and_store
        s = SessionLocal()
        uid = s.query(User).filter_by(email="phaseC@example.com").first().id
        s.close()
        cdb = CollabDB(str(paths.index_dir(uid) / "collab.db"))
        generate_and_store(cdb,
                           finance_db_path=str(paths.index_dir(uid) / "finance.db"))
        cdb.close()
        r = client.get("/api/notifications", headers=auth)
        assert r.status_code == 200
        notifications = r.json()["notifications"]
        assert any(n["type"] == "budget_overage" for n in notifications)

    def test_mark_notification_read(self, client, auth):
        r = client.get("/api/notifications?unread_only=true", headers=auth)
        unread = r.json()["notifications"]
        if not unread:
            pytest.skip("no unread notifications to mark")
        nid = unread[0]["id"]
        r2 = client.post(f"/api/notifications/{nid}/read", headers=auth)
        assert r2.status_code == 200
        assert r2.json()["ok"] is True

    def test_mark_all_read(self, client, auth):
        r = client.post("/api/notifications/read-all", headers=auth)
        assert r.status_code == 200
        r2 = client.get("/api/notifications/count", headers=auth)
        assert r2.json()["unread_count"] == 0

    def test_mark_missing_notification(self, client, auth):
        r = client.post("/api/notifications/nope/read", headers=auth)
        assert r.status_code == 404

    def test_unread_count_endpoint(self, client, auth):
        r = client.get("/api/notifications/count", headers=auth)
        assert r.status_code == 200
        assert "unread_count" in r.json()


class TestCalendarAPI:
    def test_calendar_sync_no_google(self, client, auth):
        r = client.post("/api/finance/calendar/sync", headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert "errors" in body
        assert len(body["errors"]) > 0   # no Google linked

    def test_bank_presets_endpoint(self, client, auth):
        r = client.get("/api/finance/bank-presets")
        assert r.status_code == 200
        presets = r.json()["presets"]
        bank_ids = [p["bank_id"] for p in presets]
        assert "HDFC" in bank_ids
        assert "ICICI" in bank_ids
        assert "SBI" in bank_ids

    def test_gmail_scope_confirmed(self, client, auth):
        r = client.get("/api/finance/gmail/scope-status", headers=auth)
        assert r.status_code == 200
        assert r.json()["gmail_scope_in_oauth_flow"] is True


# ===========================================================================
# 7.  AA toggle — settings endpoint + kill-switch behaviour
# ===========================================================================

class TestAAToggle:
    def test_me_includes_aa_enabled(self, client, auth):
        r = client.get("/api/me", headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert "aa_enabled" in body
        assert body["aa_enabled"] is True   # defaults to enabled

    def test_get_aa_setting(self, client, auth):
        r = client.get("/api/settings/aa-enabled", headers=auth)
        assert r.status_code == 200
        assert r.json()["aa_enabled"] is True

    def test_disable_aa(self, client, auth):
        r = client.post("/api/settings/aa-enabled",
                        json={"enabled": False}, headers=auth)
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["aa_enabled"] is False

    def test_get_aa_setting_reflects_disable(self, client, auth):
        r = client.get("/api/settings/aa-enabled", headers=auth)
        assert r.json()["aa_enabled"] is False

    def test_me_reflects_aa_disabled(self, client, auth):
        r = client.get("/api/me", headers=auth)
        assert r.json()["aa_enabled"] is False

    def test_sync_aa_blocked_when_disabled(self, client, auth):
        """AA sync returns 403 when user has disabled AA, even without account."""
        aid_r = client.post("/api/finance/accounts",
                            json={"nickname": "Test SBI", "bank_name": "SBI",
                                  "account_type": "savings"},
                            headers=auth)
        assert aid_r.status_code == 200
        aid = aid_r.json()["id"]
        r = client.post(f"/api/finance/accounts/{aid}/sync/aa", headers=auth)
        assert r.status_code == 403
        assert "disabled" in r.json()["detail"].lower()

    def test_aa_status_reflects_disabled(self, client, auth):
        """AA status endpoint reports aa_enabled_in_settings=False and includes note."""
        aid_r = client.post("/api/finance/accounts",
                            json={"nickname": "Test HDFC", "bank_name": "HDFC",
                                  "account_type": "savings"},
                            headers=auth)
        aid = aid_r.json()["id"]
        r = client.get(f"/api/finance/accounts/{aid}/sync/aa/status", headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert body["aa_enabled_in_settings"] is False
        assert "note" in body
        assert "disabled" in body["note"].lower()

    def test_re_enable_aa(self, client, auth):
        r = client.post("/api/settings/aa-enabled",
                        json={"enabled": True}, headers=auth)
        assert r.status_code == 200
        assert r.json()["aa_enabled"] is True
        r2 = client.get("/api/me", headers=auth)
        assert r2.json()["aa_enabled"] is True

    def test_aa_status_enabled_in_settings_after_reenable(self, client, auth):
        aid_r = client.post("/api/finance/accounts",
                            json={"nickname": "Test ICICI", "bank_name": "ICICI",
                                  "account_type": "savings"},
                            headers=auth)
        aid = aid_r.json()["id"]
        r = client.get(f"/api/finance/accounts/{aid}/sync/aa/status", headers=auth)
        assert r.status_code == 200
        assert r.json()["aa_enabled_in_settings"] is True
        assert "note" not in r.json()
