"""CONTEXT_PLAN C3 — commitments engine: detection, ladder, expiry."""
import datetime as _dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.collab import CollabDB
from amy.commitments import CommitmentEngine, commitment_scan


def _days_ago(n: int) -> str:
    return (_dt.date.today() - _dt.timedelta(days=n)).isoformat()


def _days_ahead(n: int) -> str:
    return (_dt.date.today() + _dt.timedelta(days=n)).isoformat()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    cdb = CollabDB(str(tmp_path / "collab.db"))
    ctx = build_ctx("u-commit", "t@example.com", cdb, tmp_path, llm_router=None)
    yield ctx, cdb
    cdb.close()


def test_return_window_detected_and_idempotent(env):
    ctx, _ = env
    fe = ctx.open_finance()
    try:
        fe.add_transaction(-2500, "Shopping", "AMAZON PAY INDIA",
                           date=_days_ago(2))
        ce = CommitmentEngine(fe)
        created = ce.detect()
        assert len(created) == 1
        c = ce.list("open")[0]
        assert c["kind"] == "return_window"
        assert c["due_date"] == _days_ago(2 - 10)   # purchase + 10d (Amazon)
        assert c["source"] == "auto"
        assert ce.detect() == []                    # idempotent per txn+kind
    finally:
        fe.close()


def test_return_window_skipped_when_already_closed(env):
    ctx, _ = env
    fe = ctx.open_finance()
    try:
        fe.add_transaction(-900, "Shopping", "MEESHO", date=_days_ago(20))
        assert CommitmentEngine(fe).detect() == []   # 7d window long gone
    finally:
        fe.close()


def test_warranty_by_category_and_amount(env):
    ctx, _ = env
    fe = ctx.open_finance()
    try:
        fe.add_transaction(-3000, "Electronics", "LOCAL TV SHOP",
                           date=_days_ago(1))
        fe.add_transaction(-45000, "Furniture", "SOFA WORLD", date=_days_ago(1))
        fe.add_transaction(-45000, "Transfer", "SELF", date=_days_ago(1))
        ce = CommitmentEngine(fe)
        ce.detect()
        kinds = [(c["kind"], c["merchant"]) for c in ce.list("open")]
        assert ("warranty", "LOCAL TV SHOP") in kinds       # category rule
        assert ("warranty", "SOFA WORLD") in kinds          # amount rule
        assert not any(m == "SELF" for _, m in kinds)       # transfers skipped
    finally:
        fe.close()


def test_scan_job_ladder_and_expiry(env):
    ctx, _ = env
    fe = ctx.open_finance()
    try:
        ce = CommitmentEngine(fe)
        ce.add("document", "Passport renewal", _days_ahead(2))     # ≤3d high
        ce.add("warranty", "Fridge warranty", _days_ahead(10))     # 4–14d normal
        ce.add("return_window", "Return X", _days_ahead(10))       # no 14d rung
        ce.add("custom", "Old thing", _days_ago(1))                # → expired
    finally:
        fe.close()

    out = commitment_scan(ctx)
    assert out["expired"] == 1
    notifs = ctx.notify_store().list()
    due_soon = [n for n in notifs if n["type"] == "commitment_due_soon"]
    upcoming = [n for n in notifs if n["type"] == "commitment_upcoming"]
    assert len(due_soon) == 1 and "Passport" in due_soon[0]["title"]
    assert due_soon[0]["priority"] == "high"
    assert len(upcoming) == 1 and "Fridge" in upcoming[0]["title"]

    out2 = commitment_scan(ctx)                     # same day → all deduped
    assert out2["notified"] == 0


def test_scan_job_detects_and_announces(env):
    ctx, _ = env
    fe = ctx.open_finance()
    try:
        fe.add_transaction(-1500, "Shopping", "MYNTRA DESIGNS",
                           date=_days_ago(1))
    finally:
        fe.close()
    out = commitment_scan(ctx)
    assert out["created"] == 1
    notifs = ctx.notify_store().list()
    assert any(n["type"] == "commitment_created" and "MYNTRA" in n["title"]
               for n in notifs)


def test_manual_lifecycle(env):
    ctx, _ = env
    fe = ctx.open_finance()
    try:
        ce = CommitmentEngine(fe)
        cid = ce.add("custom", "Renew driving licence", _days_ahead(30),
                     notes="RTO online")
        assert ce.update(cid, status="done")
        assert ce.list("open") == []
        assert ce.list("done")[0]["id"] == cid
        assert ce.delete(cid) and not ce.list("all")
    finally:
        fe.close()
