"""CAREER AUTOPILOT Phase E — Opportunity Radar: HN "Who's Hiring" +
GitHub org activity + honest Product Hunt/Reddit stubs, deterministic
scoring, and never-recompute explain.

All comments/repos/postings/profiles constructed here are SYNTHETIC test
fixtures, not real career data. See amy/opportunity_radar.py's module
docstring for the LinkedIn hard-ban and the score-once-at-discovery rule.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.career_portfolio import persist_classification
from amy.collab import CollabDB
from amy.opportunity_radar import (
    OpportunityRadarSensor, _scan_github_org_activity, _scan_hn_whos_hiring,
    _scan_product_hunt, _scan_reddit, explain_opportunity_score,
    list_opportunities, score_opportunity,
)


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-radar", "radar@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _recent_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _repo(name, description="", pushed_at=None):
    return {"name": name, "description": description, "pushed_at": pushed_at or _recent_iso()}


def _seed_matched_company(ctx, company, score=85.0):
    posting = {"title": "Flutter Developer", "company": company,
              "url": f"https://x/{company}", "location": "Remote", "salary": "",
              "is_remote": True, "description": "Requires Docker.", "source": "jobspy",
              "keywords": ["docker"]}
    pid, _ = ctx.store.add_posting_if_new(ctx.user_id, posting)
    ctx.store.set_posting_match(ctx.user_id, pid, score, {})


# ---------------------------------------------------------------------------
# Sensor guard — same no-active-goal/no-target-role precedent as JobScoutSensor
# ---------------------------------------------------------------------------

def test_poll_noop_without_active_career_goal(ctx):
    sensor = OpportunityRadarSensor(ctx.events(), ctx)
    assert sensor.poll() == []


def test_poll_noop_without_target_role(ctx):
    from amy.autonomous import GoalEngine
    GoalEngine(ctx.collab).create_goal("Vague goal", domain="career")
    sensor = OpportunityRadarSensor(ctx.events(), ctx)
    assert sensor.poll() == []


# ---------------------------------------------------------------------------
# score_opportunity — every reason traces to a real computed value
# ---------------------------------------------------------------------------

def test_score_no_skill_match_reason_without_keywords(ctx):
    ctx.store.set_career_profile(ctx.user_id, skills=["Docker"])
    out = score_opportunity(ctx, "", "", "Acme", "hackernews_whos_hiring",
                            "hiring_signal_detected")
    assert not any(r.startswith("skill_match_") for r in out["reasons"])
    assert not any(r.startswith("portfolio_evidence_") for r in out["reasons"])
    assert out["reasons"] == ["hiring_signal_detected"]


def test_score_includes_real_skill_match_and_portfolio_evidence(ctx):
    ctx.store.set_career_profile(ctx.user_id, skills=["Docker"])
    persist_classification(ctx, "Flutter Developer",
                           [_repo("acme-app")], [], [],
                           entries_by_repo={})
    # give the showcase item real matched_keywords overlapping "docker"
    ctx.store.upsert_portfolio_item(ctx.user_id, "acme-app", "showcase",
                                    matched_keywords=["docker"])
    out = score_opportunity(ctx, "Flutter Developer", "Requires Docker experience.",
                            "Acme", "hackernews_whos_hiring", "hiring_signal_detected")
    assert any(r.startswith("skill_match_") for r in out["reasons"])
    assert any(r.startswith("portfolio_evidence_") for r in out["reasons"])
    assert 0 < out["score"] <= 100
    assert out["recommended_action"] in ("apply_soon", "review", "monitor")


def test_score_never_includes_funding_or_blog_reasons(ctx):
    ctx.store.set_career_profile(ctx.user_id, skills=["Docker", "AWS", "Kubernetes"])
    out = score_opportunity(ctx, "Flutter Developer", "Requires Docker, AWS, Kubernetes.",
                            "Acme", "github", "github_org_activity_detected")
    blob = " ".join(out["reasons"]).lower()
    for banned in ("funding", "layoff", "acquisition", "blog", "momentum"):
        assert banned not in blob


# ---------------------------------------------------------------------------
# HN "Who's Hiring" — real discovery + honest unavailable
# ---------------------------------------------------------------------------

def test_hn_whos_hiring_discovers_and_scores(ctx, monkeypatch):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer",
                                 skills=["Docker"])

    def fake_call(uid, store, source, candidates, args, target_style="owner_repo"):
        assert source == "hackernews"
        return {"result": {"structured": [
            {"title": "Acme | Remote | Flutter Developer — requires Docker.",
             "url": "https://news.ycombinator.com/item?id=1",
             "summary": "Acme | Remote | Flutter Developer — requires Docker.",
             "points": 10, "created_at": "2026-01-01T00:00:00Z"}]}}

    monkeypatch.setattr("amy.connectors.mcp_call.call_mcp_tool", fake_call)

    out = _scan_hn_whos_hiring(ctx, "Flutter Developer")
    assert out == {"available": True, "discovered": 1}

    postings = ctx.store.list_postings(ctx.user_id)
    hn_postings = [p for p in postings if p["source"] == "hn_whos_hiring"]
    assert len(hn_postings) == 1
    assert hn_postings[0]["match_score"] is not None
    assert hn_postings[0]["match_factors"]["source"] == "hackernews_whos_hiring"

    # a second scan with the SAME comment doesn't re-discover (URL dedup)
    out2 = _scan_hn_whos_hiring(ctx, "Flutter Developer")
    assert out2 == {"available": True, "discovered": 0}


def test_hn_whos_hiring_unavailable_without_connector(ctx, monkeypatch):
    from amy.connectors.mcp_call import ConnectorCallError

    def fake_call(*a, **kw):
        raise ConnectorCallError("no hackernews connector registered")

    monkeypatch.setattr("amy.connectors.mcp_call.call_mcp_tool", fake_call)
    out = _scan_hn_whos_hiring(ctx, "Flutter Developer")
    assert out["available"] is False


# ---------------------------------------------------------------------------
# Product Hunt / Reddit — generic honest stubs
# ---------------------------------------------------------------------------

def test_product_hunt_unavailable_without_connector(ctx, monkeypatch):
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, name: None)
    assert _scan_product_hunt(ctx, "Flutter Developer") == {"available": False}


def test_reddit_unavailable_without_connector(ctx, monkeypatch):
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, name: None)
    assert _scan_reddit(ctx, "Flutter Developer") == {"available": False}


# ---------------------------------------------------------------------------
# GitHub org activity — grounded in real Phase B match data, cursor-deduped
# ---------------------------------------------------------------------------

def test_github_activity_honest_empty_without_matched_companies(ctx, monkeypatch):
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, name: SimpleNamespace(default_target="me/repo"))
    out = _scan_github_org_activity(ctx, "Flutter Developer")
    assert out == {"available": True, "detected": 0,
                   "reason": "no matched companies on file yet"}


def test_github_activity_detects_and_dedups_on_same_pushed_at(ctx, monkeypatch):
    ctx.store.set_career_profile(ctx.user_id, skills=["Docker"])
    _seed_matched_company(ctx, "Acme", score=90.0)
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, name: SimpleNamespace(default_target="me/repo"))

    import amy.tools as amy_tools

    pushed_at = _recent_iso()

    def fake_invoke(ctx, name, args, actor="agent"):
        assert name == "portfolio_repo_list"
        return {"repos": [_repo("acme-infra", description="Docker infra",
                               pushed_at=pushed_at)]}

    monkeypatch.setattr(amy_tools, "invoke", fake_invoke)

    out = _scan_github_org_activity(ctx, "Flutter Developer")
    assert out == {"available": True, "detected": 1}
    signals = ctx.store.list_opportunity_signals(ctx.user_id)
    assert len(signals) == 1
    assert signals[0]["company"] == "Acme"

    # same pushed_at again — already signaled, no duplicate
    out2 = _scan_github_org_activity(ctx, "Flutter Developer")
    assert out2 == {"available": True, "detected": 0}
    assert len(ctx.store.list_opportunity_signals(ctx.user_id)) == 1


# ---------------------------------------------------------------------------
# list_opportunities / explain_opportunity_score — stored data only
# ---------------------------------------------------------------------------

def test_list_opportunities_merges_postings_and_signals(ctx, monkeypatch):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer",
                                 skills=["Docker"])

    def fake_hn(uid, store, source, candidates, args, target_style="owner_repo"):
        return {"result": {"structured": [
            {"title": "Acme | Remote | Flutter Developer", "url": "https://hn/1",
             "summary": "Requires Docker.", "points": 5, "created_at": "2026-01-01"}]}}

    monkeypatch.setattr("amy.connectors.mcp_call.call_mcp_tool", fake_hn)
    _scan_hn_whos_hiring(ctx, "Flutter Developer")

    ctx.store.create_opportunity_signal(
        ctx.user_id, "github", "Beta", "github_org_activity",
        {"company": "Beta", "score": 40.0, "reasons": ["github_org_activity_detected"],
         "source": "github", "detected_at": "2026-01-02T00:00:00Z",
         "recommended_action": "monitor"}, 40.0)

    out = list_opportunities(ctx)
    kinds = {o["id"].split(":")[0] for o in out}
    assert kinds == {"posting", "signal"}
    assert len(out) == 2

    only_github = list_opportunities(ctx, source="github")
    assert len(only_github) == 1
    assert only_github[0]["company"] == "Beta"


def test_explain_opportunity_score_never_recomputes(ctx, monkeypatch):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer",
                                 skills=["Docker"])

    def fake_hn(uid, store, source, candidates, args, target_style="owner_repo"):
        return {"result": {"structured": [
            {"title": "Acme | Remote | Flutter Developer", "url": "https://hn/1",
             "summary": "Requires Docker.", "points": 5, "created_at": "2026-01-01"}]}}

    monkeypatch.setattr("amy.connectors.mcp_call.call_mcp_tool", fake_hn)
    _scan_hn_whos_hiring(ctx, "Flutter Developer")
    posting = [p for p in ctx.store.list_postings(ctx.user_id)
              if p["source"] == "hn_whos_hiring"][0]
    stored_score = posting["match_score"]

    # change the profile AFTER discovery — a live recompute would change
    # the skill-match reason; explain must still show the ORIGINAL score
    ctx.store.set_career_profile(ctx.user_id, skills=[])

    out = explain_opportunity_score(ctx, f"posting:{posting['id']}")
    assert out["available"] is True
    assert out["score"] == stored_score
    assert any(r.startswith("skill_match_") for r in out["reasons"])


def test_explain_opportunity_score_unknown_id(ctx):
    out = explain_opportunity_score(ctx, "posting:does-not-exist")
    assert out["available"] is False
