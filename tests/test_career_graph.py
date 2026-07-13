"""CAREER AUTOPILOT Phase B — Career Intelligence Graph: graph population
+ skill-gap roadmap + company matching + rejection analysis.

All postings/applications/profiles constructed in this file are
SYNTHETIC test fixtures, not real career data. See amy/career_graph.py's
module docstring for the shared-graph-not-dedicated reasoning and the
honesty rules each query function follows.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.career_graph import (
    companies_matching_profile, rebuild_career_graph, top_skill_gap, why_rejected,
)
from amy.career_scout import _extract_posting_keywords
from amy.collab import CollabDB
from amy.knowledge_graph.store import GraphStore


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-careergraph", "careergraph@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _seed_posting(ctx, title, company, description, match_score=None):
    posting = {"title": title, "company": company, "url": f"https://x/{title}-{company}",
              "location": "Remote", "salary": "", "is_remote": True,
              "description": description, "source": "jobspy",
              "keywords": _extract_posting_keywords(title, description)}
    pid, is_new = ctx.store.add_posting_if_new(ctx.user_id, posting)
    assert is_new
    if match_score is not None:
        ctx.store.set_posting_match(ctx.user_id, pid, match_score, {})
    return pid


def _graph_path(ctx):
    from pathlib import Path as _P
    return str(_P(ctx.finance_path).parent / "graph.db")


# ---------------------------------------------------------------------------
# Graph population — shared graph.db, no reset
# ---------------------------------------------------------------------------

def test_rebuild_skipped_without_target_role(ctx):
    assert rebuild_career_graph(ctx) == {"skipped": "no target_role on file"}


def test_rebuild_never_resets_unrelated_nodes(ctx):
    # simulate a pre-existing orchestrator.py plan-graph node in the SAME
    # shared graph.db, the way _store_plan_graph() would leave one
    g = GraphStore(_graph_path(ctx))
    g.add_node("agentgoal:run1", "goal", "Unrelated plan-graph goal")
    g.commit()
    g.close()

    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer", skills=[])
    _seed_posting(ctx, "Flutter Developer - A", "Acme", "Requires Docker.", match_score=80)
    rebuild_career_graph(ctx)

    g = GraphStore(_graph_path(ctx))
    node = g.get_node("agentgoal:run1")
    g.close()
    assert node is not None
    assert node["label"] == "Unrelated plan-graph goal"


def test_rebuild_creates_requires_edges_excluding_track_name(ctx):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer", skills=[])
    _seed_posting(ctx, "Flutter Developer - A", "Acme", "Requires Docker and AWS.", match_score=85)
    _seed_posting(ctx, "Flutter Developer - B", "Acme", "Requires Docker.", match_score=60)

    result = rebuild_career_graph(ctx)
    assert result["tracks"] == ["Flutter Developer"]

    g = GraphStore(_graph_path(ctx))
    try:
        edges = g.edges()
        requires = [e for e in edges if e["rel"] == "requires"]
        skill_ids = {e["dst"] for e in requires if e["src"] == "company:acme"}
        assert "skill:docker" in skill_ids
        assert "skill:aws" in skill_ids
        # the track's own name isn't a skill
        assert "skill:flutter" not in skill_ids
        assert "skill:developer" not in skill_ids

        matched_by = [e for e in edges if e["rel"] == "matched_by"]
        acme_edge = next(e for e in matched_by if e["dst"] == "company:acme")
        assert acme_edge["src"] == "role:flutter developer"
        assert acme_edge["weight"] == pytest.approx((85 + 60) / 2)
    finally:
        g.close()


def test_rebuild_creates_applied_to_edges_from_applications(ctx):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer", skills=[])
    pid = _seed_posting(ctx, "Flutter Developer - A", "Acme", "Requires Docker.", match_score=80)
    ctx.store.create_application(ctx.user_id, pid, channel="email")

    rebuild_career_graph(ctx)

    g = GraphStore(_graph_path(ctx))
    try:
        applied = [e for e in g.edges() if e["rel"] == "applied_to"]
        assert any(e["src"] == "role:flutter developer" and e["dst"] == "company:acme"
                  and e["weight"] == 1.0 for e in applied)
    finally:
        g.close()


# ---------------------------------------------------------------------------
# top_skill_gap — reuses skill_demand_report, no salary anywhere
# ---------------------------------------------------------------------------

def test_top_skill_gap_matches_skill_demand_report(ctx):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer", skills=["Dart"])
    for i in range(4):
        _seed_posting(ctx, f"Flutter Developer - {i}", "Acme", "Requires Docker experience.")

    roadmap = top_skill_gap(ctx, "Flutter Developer")
    assert roadmap["target_role"] == "Flutter Developer"
    assert roadmap["ordering_basis"] == ("demand frequency across matched postings, "
                                        "most-demanded first")
    skills = [e["skill"] for e in roadmap["missing_skills"]]
    assert "Docker" in skills
    assert [e["order"] for e in roadmap["missing_skills"]] == list(
        range(1, len(roadmap["missing_skills"]) + 1))

    # no salary/compensation key anywhere in the output
    blob = str(roadmap).lower()
    assert "salary" not in blob and "compensation" not in blob


# ---------------------------------------------------------------------------
# companies_matching_profile — reuses stored match_score, never recomputes
# ---------------------------------------------------------------------------

def test_companies_matching_profile_excludes_unscored_and_ranks_correctly(ctx):
    _seed_posting(ctx, "Flutter Developer - A", "HighScorer", "...", match_score=90)
    _seed_posting(ctx, "Flutter Developer - B", "HighScorer", "...", match_score=85)
    _seed_posting(ctx, "Flutter Developer - C", "LowScorer", "...", match_score=40)
    _seed_posting(ctx, "Flutter Developer - D", "NeverScored", "...", match_score=None)

    out = companies_matching_profile(ctx, min_avg_score=70.0)
    names = [c["company"] for c in out["companies"]]
    assert names == ["HighScorer"]   # LowScorer below threshold, NeverScored excluded entirely
    assert out["companies"][0]["scored_postings"] == 2
    assert out["companies"][0]["avg_match_score"] == pytest.approx(87.5)


# ---------------------------------------------------------------------------
# why_rejected — graded, never a confident cause
# ---------------------------------------------------------------------------

def test_why_rejected_none_when_not_rejected(ctx):
    pid = _seed_posting(ctx, "Flutter Developer - A", "Acme", "Requires Docker.")
    aid = ctx.store.create_application(ctx.user_id, pid, channel="email")
    out = why_rejected(ctx, aid)
    assert out["available"] is False


def test_why_rejected_none_confidence_when_no_missing_skills(ctx):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer",
                                 skills=["Docker"])
    pid = _seed_posting(ctx, "Flutter Developer - A", "Acme", "Requires Docker.")
    aid = ctx.store.create_application(ctx.user_id, pid, channel="email")
    ctx.store.update_application_status(ctx.user_id, aid, "rejected")

    out = why_rejected(ctx, aid)
    assert out["available"] is True
    assert out["confidence"] == "none"
    assert out["missing_skills"] == []
    assert "current" in out["explanation"].lower()


def test_why_rejected_graded_confidence_when_skills_missing(ctx):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer", skills=[])
    pid = _seed_posting(ctx, "Flutter Developer - A", "Acme",
                        "Requires Docker, AWS, and Kotlin experience.")
    aid = ctx.store.create_application(ctx.user_id, pid, channel="email")
    ctx.store.update_application_status(ctx.user_id, aid, "rejected")

    out = why_rejected(ctx, aid)
    assert out["available"] is True
    assert out["confidence"] in ("low", "moderate")
    assert out["confidence"] != "high"   # never a confident cause
    assert "Docker" in out["missing_skills"]
