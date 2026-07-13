"""CAREER AUTOPILOT Phase F — Interview Memory: manually-logged journal +
retrospective pattern analysis, NOT passive detection.

All applications/postings/interview logs constructed here are SYNTHETIC
test fixtures, not real career data. See amy/interview_memory.py's
module docstring for the company-derivation and tier-1 reasoning.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.career_scout import _extract_posting_keywords
from amy.collab import CollabDB
from amy.interview_memory import (
    interview_patterns, interview_weakness_report, log_interview,
    log_interview_from_chat,
)
from amy.knowledge_graph.store import GraphStore


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    # keep tests fast/offline — none of these care about real LLM output,
    # only the honest-fallback behavior when structuring fails
    monkeypatch.setattr("amy.agents.reactive._get_llm", lambda ctx: None)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-interview", "interview@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _seed_application(ctx, company, status="interview"):
    posting = {"title": "Flutter Developer", "company": company,
              "url": f"https://x/{company}", "location": "Remote", "salary": "",
              "is_remote": True, "description": "Requires Docker.", "source": "jobspy",
              "keywords": _extract_posting_keywords("Flutter Developer", "Requires Docker.")}
    pid, _ = ctx.store.add_posting_if_new(ctx.user_id, posting)
    aid = ctx.store.create_application(ctx.user_id, pid, channel="email")
    ctx.store.update_application_status(ctx.user_id, aid, status)
    return aid


def _graph_path(ctx):
    from amy.career_graph import _graph_path as gp
    return gp(ctx)


def _seed_skill_node(ctx, tag):
    g = GraphStore(_graph_path(ctx))
    g.add_node(f"skill:{tag.lower()}", "skill", tag)
    g.commit()
    g.close()


# ---------------------------------------------------------------------------
# log_interview — validation, company derivation, tier 1
# ---------------------------------------------------------------------------

def test_log_interview_rejects_unknown_round_type(ctx):
    with pytest.raises(ValueError):
        log_interview(ctx, company="Acme", round_type="not_a_real_type")


def test_log_interview_rejects_unknown_outcome(ctx):
    with pytest.raises(ValueError):
        log_interview(ctx, company="Acme", self_assessed_outcome="terrible")


def test_log_interview_derives_company_from_application_not_caller(ctx):
    aid = _seed_application(ctx, "RealCompany")
    log_interview(ctx, application_id=aid, company="WrongGuess", round_type="technical")
    logs = ctx.store.list_interview_logs(ctx.user_id)
    assert logs[0]["company"] == "RealCompany"


def test_log_interview_company_only_without_application_id(ctx):
    log_interview(ctx, company="ReferralChat Inc", round_type="other")
    logs = ctx.store.list_interview_logs(ctx.user_id)
    assert logs[0]["company"] == "ReferralChat Inc"
    assert logs[0]["application_id"] is None


def test_log_interview_is_tier1_auto_executed(ctx):
    out = log_interview(ctx, company="Acme", round_type="technical")
    assert out["status"] == "auto_executed"


def test_log_interview_unknown_application_id_honest_error(ctx):
    out = log_interview(ctx, application_id="does-not-exist", company="Acme")
    assert "error" in out
    assert ctx.store.list_interview_logs(ctx.user_id) == []


# ---------------------------------------------------------------------------
# log_interview_from_chat — honest fallback, never invents
# ---------------------------------------------------------------------------

def test_log_interview_from_chat_honest_fallback_without_llm(ctx):
    out = log_interview_from_chat(ctx, "Acme", "It went okay, mostly small talk.")
    assert out["status"] == "auto_executed"
    logs = ctx.store.list_interview_logs(ctx.user_id)
    log = logs[0]
    assert log["round_type"] == "other"
    assert log["questions"] == []
    assert log["weakness_tags"] == []
    assert log["self_assessed_outcome"] == "ok"
    assert log["notes"] == "It went okay, mostly small talk."


def test_log_interview_from_chat_resolves_application_by_company(ctx):
    aid = _seed_application(ctx, "Acme Corp")
    log_interview_from_chat(ctx, "Acme Corp", "Had the call today.")
    logs = ctx.store.list_interview_logs(ctx.user_id)
    assert logs[0]["application_id"] == aid


def test_log_interview_from_chat_no_match_stays_unlinked(ctx):
    log_interview_from_chat(ctx, "Totally Unrelated Startup", "Had the call today.")
    logs = ctx.store.list_interview_logs(ctx.user_id)
    assert logs[0]["application_id"] is None


# ---------------------------------------------------------------------------
# interview_patterns — retrospective aggregation, real skill-graph links only
# ---------------------------------------------------------------------------

def test_interview_patterns_aggregates_weaknesses_and_outcomes(ctx):
    log_interview(ctx, company="A", round_type="technical",
                 self_assessed_outcome="weak", weakness_tags=["system_design"])
    log_interview(ctx, company="B", round_type="technical",
                 self_assessed_outcome="strong", weakness_tags=["system_design"])
    log_interview(ctx, company="C", round_type="behavioral",
                 self_assessed_outcome="ok", weakness_tags=["communication"])

    out = interview_patterns(ctx)
    assert out["total_logged"] == 3

    weaknesses = {w["tag"]: w for w in out["recurring_weaknesses"]}
    assert weaknesses["system_design"]["count"] == 2
    assert weaknesses["system_design"]["pct_of_interviews"] == pytest.approx(66.7, abs=0.1)
    assert weaknesses["communication"]["count"] == 1

    assert out["outcome_by_round_type"]["technical"] == {"strong": 1, "ok": 0, "weak": 1}
    assert out["outcome_by_round_type"]["behavioral"] == {"strong": 0, "ok": 1, "weak": 0}


def test_interview_patterns_linked_skill_gaps_only_real_matches(ctx):
    _seed_skill_node(ctx, "Kubernetes")
    log_interview(ctx, company="A", round_type="technical",
                 weakness_tags=["Kubernetes", "made_up_nonexistent_skill"])

    out = interview_patterns(ctx)
    assert out["linked_skill_gaps"] == ["Kubernetes"]


def test_interview_patterns_empty_honest_zero(ctx):
    out = interview_patterns(ctx)
    assert out == {"total_logged": 0, "recurring_weaknesses": [],
                   "outcome_by_round_type": {}, "linked_skill_gaps": []}


def test_interview_weakness_report_reuses_patterns_verbatim(ctx):
    _seed_skill_node(ctx, "Kubernetes")
    for _ in range(3):
        log_interview(ctx, company="A", round_type="technical",
                      weakness_tags=["Kubernetes"])
    patterns = interview_patterns(ctx)
    report = interview_weakness_report(ctx)
    assert report["recurring_weaknesses"] == patterns["recurring_weaknesses"]
    assert report["linked_skill_gaps"] == patterns["linked_skill_gaps"]
    assert "Kubernetes" in report["summary"] or "3" in report["summary"]
