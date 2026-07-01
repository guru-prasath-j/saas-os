"""Tests for Phase B1: accounts, column maps, and CSV import."""
from __future__ import annotations

import io
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Unit tests — FinanceEngine accounts & column maps
# ---------------------------------------------------------------------------

@pytest.fixture()
def engine(tmp_path):
    from amy.finance.engine import FinanceEngine
    fe = FinanceEngine(str(tmp_path / "finance.db"))
    yield fe
    fe.close()


class TestAccounts:
    def test_add_and_get(self, engine):
        aid = engine.add_account("HDFC Savings", "HDFC", "savings", "manual")
        acc = engine.get_account(aid)
        assert acc is not None
        assert acc["nickname"] == "HDFC Savings"
        assert acc["bank_name"] == "HDFC"
        assert acc["account_type"] == "savings"
        assert acc["sync_method"] == "manual"
        assert acc["last_synced_at"] is None
        assert acc["meta"] == {}

    def test_list_empty(self, engine):
        assert engine.list_accounts() == []

    def test_list_multiple(self, engine):
        engine.add_account("A", "BankA", "savings")
        engine.add_account("B", "BankB", "credit_card")
        accounts = engine.list_accounts()
        assert len(accounts) == 2
        names = {a["nickname"] for a in accounts}
        assert names == {"A", "B"}

    def test_list_includes_transaction_count(self, engine):
        aid = engine.add_account("HDFC", "HDFC", "savings")
        engine.add_transaction(-500, "Food", "Swiggy", account_id=aid)
        engine.add_transaction(-200, "Food", "Zomato", account_id=aid)
        accounts = engine.list_accounts()
        assert accounts[0]["transaction_count"] == 2

    def test_update(self, engine):
        aid = engine.add_account("Old", "Bank", "savings")
        ok = engine.update_account(aid, nickname="New Name", sync_method="csv")
        assert ok
        acc = engine.get_account(aid)
        assert acc["nickname"] == "New Name"
        assert acc["sync_method"] == "csv"

    def test_update_invalid_type(self, engine):
        aid = engine.add_account("A", "Bank", "savings")
        with pytest.raises(ValueError):
            engine.update_account(aid, account_type="not_a_type")

    def test_update_invalid_sync(self, engine):
        aid = engine.add_account("A", "Bank", "savings")
        with pytest.raises(ValueError):
            engine.update_account(aid, sync_method="ftp")

    def test_delete_unlinks_transactions(self, engine):
        aid = engine.add_account("X", "BankX", "savings")
        engine.add_transaction(-100, "Food", "McD", account_id=aid)
        ok = engine.delete_account(aid)
        assert ok
        assert engine.get_account(aid) is None
        # transaction still exists but account_id is NULL
        txns = engine.list_transactions(account_id=None)
        assert len(txns) == 1
        assert txns[0]["account_id"] is None

    def test_delete_missing_returns_false(self, engine):
        assert not engine.delete_account("nonexistent")

    def test_touch_account(self, engine):
        aid = engine.add_account("T", "BankT", "savings")
        assert engine.get_account(aid)["last_synced_at"] is None
        engine.touch_account(aid)
        assert engine.get_account(aid)["last_synced_at"] is not None

    def test_meta_persisted(self, engine):
        aid = engine.add_account("M", "Bank", "savings", meta={"branch": "Chennai"})
        acc = engine.get_account(aid)
        assert acc["meta"] == {"branch": "Chennai"}

    def test_account_id_migration_idempotent(self, tmp_path):
        """Running FinanceEngine twice on the same db must not fail."""
        from amy.finance.engine import FinanceEngine
        db_path = str(tmp_path / "migtest.db")
        fe1 = FinanceEngine(db_path)
        fe1.close()
        fe2 = FinanceEngine(db_path)
        fe2.close()


class TestColumnMaps:
    def test_save_and_get(self, engine):
        cmap = {"date": "Date", "description": "Narration",
                "debit": "Withdrawal Amt.", "credit": "Deposit Amt."}
        engine.save_column_map("HDFC", cmap)
        got = engine.get_column_map("HDFC")
        assert got == cmap

    def test_get_missing_returns_none(self, engine):
        assert engine.get_column_map("ICICI") is None

    def test_overwrite(self, engine):
        engine.save_column_map("HDFC", {"date": "D"})
        engine.save_column_map("HDFC", {"date": "Date2"})
        got = engine.get_column_map("HDFC")
        assert got["date"] == "Date2"

    def test_list_all(self, engine):
        engine.save_column_map("HDFC", {"date": "D"})
        engine.save_column_map("ICICI", {"date": "TXN DATE"})
        maps = engine.list_column_maps()
        banks = {m["bank_name"] for m in maps}
        assert banks == {"HDFC", "ICICI"}


# ---------------------------------------------------------------------------
# Unit tests — CSV parser
# ---------------------------------------------------------------------------

def _make_csv(rows: list[dict], headers: list[str]) -> bytes:
    lines = [",".join(headers)]
    for r in rows:
        lines.append(",".join(str(r.get(h, "")) for h in headers))
    return "\n".join(lines).encode()


class TestCSVParser:
    def test_preview_no_map(self, engine):
        from amy.finance.sync.csv_import import CSVImportProvider
        aid = engine.add_account("HDFC", "HDFC", "savings")
        csv_bytes = _make_csv(
            [{"Date": "01/06/2025", "Narration": "Swiggy", "Withdrawal Amt.": "500", "Deposit Amt.": ""}],
            ["Date", "Narration", "Withdrawal Amt.", "Deposit Amt."],
        )
        provider = CSVImportProvider()
        result = provider.import_from_bytes(csv_bytes, engine, aid)
        assert isinstance(result, dict)
        assert result["needs_mapping"] is True
        assert "Date" in result["headers"]

    def test_import_debit_credit(self, engine):
        from amy.finance.sync.csv_import import CSVImportProvider
        aid = engine.add_account("HDFC", "HDFC", "savings")
        csv_bytes = _make_csv(
            [
                {"Date": "01/06/2025", "Narration": "Swiggy",
                 "Withdrawal Amt.": "500", "Deposit Amt.": ""},
                {"Date": "02/06/2025", "Narration": "Salary",
                 "Withdrawal Amt.": "", "Deposit Amt.": "50000"},
            ],
            ["Date", "Narration", "Withdrawal Amt.", "Deposit Amt."],
        )
        cmap = {
            "date": "Date", "description": "Narration",
            "debit": "Withdrawal Amt.", "credit": "Deposit Amt.",
        }
        provider = CSVImportProvider()
        result = provider.import_from_bytes(csv_bytes, engine, aid, column_map=cmap)
        assert hasattr(result, "imported")
        assert result.imported == 2
        assert result.skipped == 0
        txns = engine.list_transactions(account_id=aid)
        assert len(txns) == 2
        amounts = {t["amount"] for t in txns}
        assert -500.0 in amounts
        assert 50000.0 in amounts

    def test_import_single_amount_with_type(self, engine):
        from amy.finance.sync.csv_import import CSVImportProvider
        aid = engine.add_account("ICICI", "ICICI", "savings")
        csv_bytes = _make_csv(
            [
                {"TXN DATE": "15-06-2025", "REMARKS": "ATM", "AMOUNT": "2000", "TYPE": "Dr"},
                {"TXN DATE": "16-06-2025", "REMARKS": "NEFT", "AMOUNT": "10000", "TYPE": "Cr"},
            ],
            ["TXN DATE", "REMARKS", "AMOUNT", "TYPE"],
        )
        cmap = {
            "date": "TXN DATE", "description": "REMARKS",
            "amount": "AMOUNT", "type": "TYPE",
        }
        provider = CSVImportProvider()
        result = provider.import_from_bytes(csv_bytes, engine, aid, column_map=cmap)
        assert result.imported == 2
        txns = engine.list_transactions(account_id=aid)
        amounts = {t["amount"] for t in txns}
        assert -2000.0 in amounts
        assert 10000.0 in amounts

    def test_deduplication(self, engine):
        from amy.finance.sync.csv_import import CSVImportProvider
        aid = engine.add_account("HDFC", "HDFC", "savings")
        csv_bytes = _make_csv(
            [{"Date": "01/06/2025", "Narration": "Swiggy",
              "Withdrawal Amt.": "500", "Deposit Amt.": ""}],
            ["Date", "Narration", "Withdrawal Amt.", "Deposit Amt."],
        )
        cmap = {"date": "Date", "description": "Narration",
                "debit": "Withdrawal Amt.", "credit": "Deposit Amt."}
        provider = CSVImportProvider()
        r1 = provider.import_from_bytes(csv_bytes, engine, aid, column_map=cmap)
        r2 = provider.import_from_bytes(csv_bytes, engine, aid)  # uses saved map
        assert r1.imported == 1
        assert r2.imported == 0
        assert r2.skipped == 1

    def test_column_map_auto_saved_and_reused(self, engine):
        from amy.finance.sync.csv_import import CSVImportProvider
        aid = engine.add_account("HDFC", "HDFC", "savings")
        cmap = {"date": "Date", "description": "Narration",
                "debit": "Withdrawal Amt.", "credit": "Deposit Amt."}
        csv1 = _make_csv(
            [{"Date": "01/06/2025", "Narration": "Test", "Withdrawal Amt.": "100", "Deposit Amt.": ""}],
            ["Date", "Narration", "Withdrawal Amt.", "Deposit Amt."],
        )
        provider = CSVImportProvider()
        provider.import_from_bytes(csv1, engine, aid, column_map=cmap)

        # Second upload — different day, no column_map supplied
        csv2 = _make_csv(
            [{"Date": "05/06/2025", "Narration": "Test2", "Withdrawal Amt.": "200", "Deposit Amt.": ""}],
            ["Date", "Narration", "Withdrawal Amt.", "Deposit Amt."],
        )
        r2 = provider.import_from_bytes(csv2, engine, aid)
        assert r2.imported == 1

    def test_unparseable_date_recorded_as_error(self, engine):
        from amy.finance.sync.csv_import import CSVImportProvider
        aid = engine.add_account("HDFC", "HDFC", "savings")
        csv_bytes = _make_csv(
            [{"Date": "not-a-date", "Narration": "X", "Withdrawal Amt.": "100", "Deposit Amt.": ""}],
            ["Date", "Narration", "Withdrawal Amt.", "Deposit Amt."],
        )
        cmap = {"date": "Date", "description": "Narration",
                "debit": "Withdrawal Amt.", "credit": "Deposit Amt."}
        provider = CSVImportProvider()
        result = provider.import_from_bytes(csv_bytes, engine, aid, column_map=cmap)
        assert result.imported == 0
        assert len(result.errors) == 1

    def test_utf8_bom_encoding(self, engine):
        from amy.finance.sync.csv_import import CSVImportProvider
        aid = engine.add_account("HDFC", "HDFC", "savings")
        content = "Date,Narration,Withdrawal Amt.,Deposit Amt.\n01/06/2025,Swiggy,500,\n"
        raw = content.encode("utf-8-sig")
        cmap = {"date": "Date", "description": "Narration",
                "debit": "Withdrawal Amt.", "credit": "Deposit Amt."}
        provider = CSVImportProvider()
        result = provider.import_from_bytes(raw, engine, aid, column_map=cmap)
        assert result.imported == 1

    def test_latin1_encoding(self, engine):
        from amy.finance.sync.csv_import import CSVImportProvider
        aid = engine.add_account("SBI", "SBI", "savings")
        content = "Date,Description,Amount\n01/01/2025,Groceries,200\n"
        raw = content.encode("latin-1")
        cmap = {"date": "Date", "description": "Description", "amount": "Amount"}
        provider = CSVImportProvider()
        result = provider.import_from_bytes(raw, engine, aid, column_map=cmap)
        assert result.imported == 1

    def test_missing_account_raises(self, engine):
        from amy.finance.sync.csv_import import CSVImportProvider
        provider = CSVImportProvider()
        with pytest.raises(ValueError, match="not found"):
            provider.import_from_bytes(b"", engine, "bad-id", column_map={})

    def test_provider_available(self):
        from amy.finance.sync.csv_import import CSVImportProvider
        assert CSVImportProvider().available()

    def test_provider_method(self):
        from amy.finance.sync.csv_import import CSVImportProvider
        assert CSVImportProvider().method == "csv"


# ---------------------------------------------------------------------------
# API integration tests
# ---------------------------------------------------------------------------

_DATA_DIR = str(Path(tempfile.mkdtemp(prefix="amy_b1_test_")))
os.environ["AMY_SAAS_DATA"] = _DATA_DIR


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from amy.saas.app import app
    from amy.saas.db import init_db
    from amy.saas import tenancy
    init_db()
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture(scope="module")
def auth_headers(client):
    r = client.post("/auth/signup",
                    json={"email": "b1test@example.com", "password": "Pass1234!"})
    assert r.status_code == 200, r.text
    uid = r.json()["user"]["id"]
    from amy.saas import tenancy
    tenancy.ensure_dirs(uid)
    token = r.json()["token"]
    return {"Authorization": f"Bearer {token}"}


class TestAccountAPI:
    def test_create_account(self, client, auth_headers):
        r = client.post("/api/finance/accounts",
                        json={"nickname": "HDFC Savings", "bank_name": "HDFC",
                              "account_type": "savings", "sync_method": "manual"},
                        headers=auth_headers)
        assert r.status_code == 200, r.text
        assert "id" in r.json()

    def test_list_accounts(self, client, auth_headers):
        r = client.get("/api/finance/accounts", headers=auth_headers)
        assert r.status_code == 200, r.text
        accounts = r.json()["accounts"]
        assert isinstance(accounts, list)

    def test_get_account(self, client, auth_headers):
        r = client.post("/api/finance/accounts",
                        json={"nickname": "ICICI", "bank_name": "ICICI"},
                        headers=auth_headers)
        aid = r.json()["id"]
        r2 = client.get(f"/api/finance/accounts/{aid}", headers=auth_headers)
        assert r2.status_code == 200
        assert r2.json()["nickname"] == "ICICI"

    def test_get_missing_account(self, client, auth_headers):
        r = client.get("/api/finance/accounts/noexist", headers=auth_headers)
        assert r.status_code == 404

    def test_update_account(self, client, auth_headers):
        r = client.post("/api/finance/accounts",
                        json={"nickname": "Old", "bank_name": "SBI"},
                        headers=auth_headers)
        aid = r.json()["id"]
        r2 = client.patch(f"/api/finance/accounts/{aid}",
                          json={"nickname": "New SBI", "sync_method": "csv"},
                          headers=auth_headers)
        assert r2.status_code == 200
        r3 = client.get(f"/api/finance/accounts/{aid}", headers=auth_headers)
        assert r3.json()["nickname"] == "New SBI"

    def test_update_invalid_type(self, client, auth_headers):
        r = client.post("/api/finance/accounts",
                        json={"nickname": "X", "bank_name": "BankX"},
                        headers=auth_headers)
        aid = r.json()["id"]
        r2 = client.patch(f"/api/finance/accounts/{aid}",
                          json={"account_type": "invalid_type"},
                          headers=auth_headers)
        assert r2.status_code == 422

    def test_delete_account(self, client, auth_headers):
        r = client.post("/api/finance/accounts",
                        json={"nickname": "Del", "bank_name": "DelBank"},
                        headers=auth_headers)
        aid = r.json()["id"]
        r2 = client.delete(f"/api/finance/accounts/{aid}", headers=auth_headers)
        assert r2.status_code == 200
        r3 = client.get(f"/api/finance/accounts/{aid}", headers=auth_headers)
        assert r3.status_code == 404

    def test_delete_missing(self, client, auth_headers):
        r = client.delete("/api/finance/accounts/nope", headers=auth_headers)
        assert r.status_code == 404


class TestCSVUploadAPI:
    @pytest.fixture()
    def account_id(self, client, auth_headers):
        r = client.post("/api/finance/accounts",
                        json={"nickname": "HDFC API", "bank_name": "HDFC_API",
                              "account_type": "savings"},
                        headers=auth_headers)
        return r.json()["id"]

    def test_upload_returns_needs_mapping(self, client, auth_headers, account_id):
        csv_bytes = b"Date,Narration,Withdrawal Amt.,Deposit Amt.\n01/06/2025,Swiggy,500,\n"
        r = client.post(
            f"/api/finance/accounts/{account_id}/upload/csv",
            files={"file": ("stmt.csv", io.BytesIO(csv_bytes), "text/csv")},
            headers=auth_headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["needs_mapping"] is True
        assert "Date" in body["headers"]

    def test_save_column_map(self, client, auth_headers, account_id):
        cmap = {
            "date": "Date", "description": "Narration",
            "debit": "Withdrawal Amt.", "credit": "Deposit Amt.",
        }
        r = client.post(
            f"/api/finance/accounts/{account_id}/column-map",
            json={"column_map": cmap},
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_upload_imports_after_mapping(self, client, auth_headers, account_id):
        # Save map first
        cmap = {
            "date": "Date", "description": "Narration",
            "debit": "Withdrawal Amt.", "credit": "Deposit Amt.",
        }
        client.post(
            f"/api/finance/accounts/{account_id}/column-map",
            json={"column_map": cmap},
            headers=auth_headers,
        )
        csv_bytes = (
            b"Date,Narration,Withdrawal Amt.,Deposit Amt.\n"
            b"01/06/2025,Swiggy,500,\n"
            b"02/06/2025,Salary,,50000\n"
        )
        r = client.post(
            f"/api/finance/accounts/{account_id}/upload/csv",
            files={"file": ("stmt.csv", io.BytesIO(csv_bytes), "text/csv")},
            headers=auth_headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "needs_mapping" not in body
        assert body["imported"] == 2

    def test_upload_deduplication_on_second_upload(self, client, auth_headers, account_id):
        cmap = {
            "date": "Date", "description": "Narration",
            "debit": "Withdrawal Amt.", "credit": "Deposit Amt.",
        }
        client.post(
            f"/api/finance/accounts/{account_id}/column-map",
            json={"column_map": cmap},
            headers=auth_headers,
        )
        csv_bytes = b"Date,Narration,Withdrawal Amt.,Deposit Amt.\n15/06/2025,Dup,300,\n"
        for _ in range(2):
            client.post(
                f"/api/finance/accounts/{account_id}/upload/csv",
                files={"file": ("stmt.csv", io.BytesIO(csv_bytes), "text/csv")},
                headers=auth_headers,
            )
        r = client.get(
            f"/api/finance/accounts/{account_id}/transactions",
            headers=auth_headers,
        )
        dups = [t for t in r.json()["transactions"] if t["merchant"] == "Dup"]
        assert len(dups) == 1

    def test_account_transactions_endpoint(self, client, auth_headers, account_id):
        r = client.get(
            f"/api/finance/accounts/{account_id}/transactions",
            headers=auth_headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert "transactions" in body
        assert "count" in body

    def test_list_column_maps(self, client, auth_headers):
        r = client.get("/api/finance/column-maps", headers=auth_headers)
        assert r.status_code == 200
        assert "column_maps" in r.json()

    def test_upload_missing_account(self, client, auth_headers):
        r = client.post(
            "/api/finance/accounts/NOPE/upload/csv",
            files={"file": ("f.csv", io.BytesIO(b""), "text/csv")},
            headers=auth_headers,
        )
        assert r.status_code == 404
