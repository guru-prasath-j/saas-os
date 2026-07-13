"""CAREER AUTOPILOT Phase D — Portfolio Builder: persisted classification
+ the GitHub-activity update trigger.

All repos/postings/profiles constructed here are SYNTHETIC test fixtures,
not real career data. See amy/career_portfolio.py's module docstring for
the pushed_at-cursor activity-signal reasoning and the always-tier-2
proposal rule.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.career_portfolio import (
    persist_classification, propose_portfolio_update, scan_github_activity,
)
from amy.collab import CollabDB


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-portfolio", "portfolio@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _repo(name, matched=None, missing=None, pushed_at="2026-01-01T00:00:00Z"):
    return {"name": name, "_matched_keywords": matched or [], "_missing": missing or [],
           "pushed_at": pushed_at}


# ---------------------------------------------------------------------------
# persist_classification
# ---------------------------------------------------------------------------

def test_persist_classification_upserts_queryable_rows(ctx):
    showcase = [_repo("acme-app", matched=["docker", "aws"])]
    needs_work = [_repo("half-done", matched=["docker"], missing=["README/description"])]
    not_relevant = [_repo("archived-thing")]
    entries_by_repo = {"acme-app": {"why": "Strong Docker/AWS project.",
                                    "bullets": ["Built with Docker", "Deployed on AWS"]}}

    n = persist_classification(ctx, "Flutter Developer", showcase, needs_work,
                               not_relevant, entries_by_repo=entries_by_repo)
    assert n == 3

    items = ctx.store.list_portfolio_items(ctx.user_id)
    by_name = {i["repo_name"]: i for i in items}
    assert by_name["acme-app"]["classification"] == "showcase"
    assert by_name["acme-app"]["why"] == "Strong Docker/AWS project."
    assert "docker" in by_name["acme-app"]["matched_keywords"]
    assert by_name["half-done"]["classification"] == "needs_work"
    assert "README/description" in by_name["half-done"]["missing"]
    assert by_name["archived-thing"]["classification"] == "not_relevant"


def test_persist_classification_filters_by_classification_arg(ctx):
    persist_classification(ctx, "Flutter Developer",
                           [_repo("showcase-repo")], [_repo("needs-work-repo")], [])
    showcase_only = ctx.store.list_portfolio_items(ctx.user_id, classification="showcase")
    assert [i["repo_name"] for i in showcase_only] == ["showcase-repo"]


# ---------------------------------------------------------------------------
# propose_portfolio_update — always tier 2 (pending), never applied directly
# ---------------------------------------------------------------------------

def test_propose_portfolio_update_always_pending(ctx):
    persist_classification(ctx, "Flutter Developer", [_repo("acme-app")], [], [])
    out = propose_portfolio_update(ctx, "acme-app", "Refreshed why", ["New bullet"])
    assert out["status"] == "pending"

    # never applied to the stored item until a human approves
    item = ctx.store.get_portfolio_item(ctx.user_id, "acme-app")
    assert item["why"] == ""


def test_propose_portfolio_update_dedups_same_day(ctx):
    persist_classification(ctx, "Flutter Developer", [_repo("acme-app")], [], [])
    first = propose_portfolio_update(ctx, "acme-app", "why v1", ["b1"])
    second = propose_portfolio_update(ctx, "acme-app", "why v2", ["b2"])
    assert first["status"] == "pending"
    assert second["status"] == "duplicate"


# ---------------------------------------------------------------------------
# scan_github_activity — honest skips, real pushed_at diffing
# ---------------------------------------------------------------------------

def test_scan_skipped_without_github_connector(ctx, monkeypatch):
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, name: None)
    assert scan_github_activity(ctx) == {"skipped": "no github connector"}


def test_scan_skipped_without_persisted_items(ctx, monkeypatch):
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, name: SimpleNamespace(default_target="me/repo"))
    out = scan_github_activity(ctx)
    assert out["skipped"] == "no persisted portfolio items yet — run portfolio analysis first"


def test_scan_proposes_only_on_real_pushed_at_change(ctx, monkeypatch):
    persist_classification(ctx, "Flutter Developer",
                           [_repo("acme-app", matched=["docker"], pushed_at="2026-01-01T00:00:00Z")],
                           [], [])
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, name: SimpleNamespace(default_target="me/repo"))

    import amy.tools as amy_tools

    def fake_invoke(ctx, name, args, actor="agent"):
        assert name == "portfolio_repo_list"
        return {"repos": [_repo("acme-app", matched=["docker"],
                                pushed_at="2026-01-01T00:00:00Z")]}

    monkeypatch.setattr(amy_tools, "invoke", fake_invoke)

    # same pushed_at as when persisted — no real activity yet
    out = scan_github_activity(ctx)
    assert out == {"checked": 1, "proposed": 0}

    def fake_invoke_changed(ctx, name, args, actor="agent"):
        return {"repos": [_repo("acme-app", matched=["docker"],
                                pushed_at="2026-02-01T00:00:00Z")]}

    monkeypatch.setattr(amy_tools, "invoke", fake_invoke_changed)
    out2 = scan_github_activity(ctx)
    assert out2 == {"checked": 1, "proposed": 1}

    # a third scan with the SAME (already-seen) new timestamp proposes nothing more
    out3 = scan_github_activity(ctx)
    assert out3 == {"checked": 1, "proposed": 0}
