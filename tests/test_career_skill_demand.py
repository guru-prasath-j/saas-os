"""CAREER AUTOPILOT Phase A — "Learning Driven by Jobs": skill-demand
aggregation over job_postings.keywords + learning-focus proposals.

All job postings constructed in this file are SYNTHETIC test fixtures,
not real discovered postings. See amy/career_scout.py's module comments
for the keyword-extraction/track-matching design this module follows.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.automation.executors import approve
from amy.career_scout import (
    SKILL_DEMAND_MAX_POSTINGS, _active_tracks, _extract_posting_keywords,
    _track_matches_posting, skill_demand_report, skill_demand_reports,
)
from amy.collab import CollabDB
from amy.learning_feed.sensor import add_focus


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-skilldemand", "skilldemand@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _seed_posting(ctx, title, description, offset_days=0):
    posting = {"title": title, "company": "Synthetic Co", "url": f"https://x/{title}-{description[:10]}",
              "location": "Remote", "salary": "", "is_remote": True,
              "description": description, "source": "jobspy",
              "keywords": _extract_posting_keywords(title, description)}
    pid, is_new = ctx.store.add_posting_if_new(ctx.user_id, posting)
    assert is_new
    if offset_days:
        import datetime as _dt
        ts = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=offset_days)).isoformat()
        ctx.collab.conn.execute("UPDATE job_postings SET discovered_at=? WHERE id=?", (ts, pid))
        ctx.collab.conn.commit()
    return pid


# ---------------------------------------------------------------------------
# Keyword extraction / track helpers
# ---------------------------------------------------------------------------

def test_extract_posting_keywords_pulls_real_terms_not_noise():
    kw = _extract_posting_keywords("Senior Flutter Developer",
                                   "We need Docker, AWS and Kotlin experience with Node.js.")
    assert "Docker" in kw and "AWS" in kw and "Kotlin" in kw and "Node.js" in kw
    assert "and" not in [k.lower() for k in kw]
    assert "experience" not in [k.lower() for k in kw]


def test_active_tracks_splits_multi_role_target_role():
    tracks = _active_tracks({"target_role": "Flutter Developer / Mobile Engineer, GenAI Engineer"})
    assert tracks == ["Flutter Developer", "Mobile Engineer", "GenAI Engineer"]
    assert _active_tracks({"target_role": ""}) == []
    assert _active_tracks({}) == []


def test_track_matches_posting_heuristic():
    posting = {"title": "Flutter Developer at Acme", "description": "Build mobile apps."}
    assert _track_matches_posting("Flutter Developer", posting) is True
    assert _track_matches_posting("GenAI Engineer", posting) is False


# ---------------------------------------------------------------------------
# Report contract, frequency, in_profile
# ---------------------------------------------------------------------------

def test_skill_demand_report_frequency_and_in_profile(ctx):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer",
                                 skills=["Flutter", "Dart"])
    _seed_posting(ctx, "Flutter Developer - A", "Requires Docker and AWS experience.")
    _seed_posting(ctx, "Flutter Developer - B", "Requires Docker and Kubernetes.")
    _seed_posting(ctx, "Flutter Developer - C", "Requires Docker.")
    _seed_posting(ctx, "Flutter Developer - D", "Requires AWS only.")

    report = skill_demand_report(ctx, "Flutter Developer", propose=False)
    assert report["track"] == "Flutter Developer"
    assert report["postings_analyzed"] == 4

    by_skill = {e["skill"]: e for e in report["top_missing_skills"]}
    assert by_skill["Docker"]["frequency_pct"] == 75.0
    assert by_skill["Docker"]["in_profile"] is False
    assert by_skill["AWS"]["frequency_pct"] == 50.0
    assert by_skill["Kubernetes"]["frequency_pct"] == 25.0
    # the track's own name ("Flutter"/"Developer") is excluded from the
    # report entirely — every matched posting mentions it by construction
    # (that's how it was matched to the track), so it's never a skill gap
    assert "Flutter" not in by_skill
    assert "Developer" not in by_skill


def test_cross_track_isolation(ctx):
    ctx.store.set_career_profile(ctx.user_id,
                                 target_role="Flutter Developer, GenAI Engineer", skills=[])
    _seed_posting(ctx, "Flutter Developer - A", "Requires Docker and Kotlin.")
    _seed_posting(ctx, "GenAI Engineer - B", "Requires LangChain and OpenAI experience.")

    flutter_report = skill_demand_report(ctx, "Flutter Developer", propose=False)
    genai_report = skill_demand_report(ctx, "GenAI Engineer", propose=False)

    flutter_skills = {e["skill"] for e in flutter_report["top_missing_skills"]}
    genai_skills = {e["skill"] for e in genai_report["top_missing_skills"]}
    assert "LangChain" not in flutter_skills
    assert "Kotlin" not in genai_skills
    assert "LangChain" in genai_skills
    assert "Kotlin" in flutter_skills


def test_window_and_cap_exclude_old_and_excess_postings(ctx, monkeypatch):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer", skills=[])
    _seed_posting(ctx, "Flutter Developer - old", "Requires COBOL.", offset_days=200)
    _seed_posting(ctx, "Flutter Developer - a", "Requires Docker.")
    _seed_posting(ctx, "Flutter Developer - b", "Requires AWS.")
    _seed_posting(ctx, "Flutter Developer - c", "Requires Kotlin.")

    # old posting excluded by the 90-day window regardless of the cap
    report = skill_demand_report(ctx, "Flutter Developer", propose=False)
    assert report["postings_analyzed"] == 3
    assert "COBOL" not in {e["skill"] for e in report["top_missing_skills"]}

    # cap applies independently of the window
    monkeypatch.setattr("amy.career_scout.SKILL_DEMAND_MAX_POSTINGS", 2)
    capped = skill_demand_report(ctx, "Flutter Developer", propose=False)
    assert capped["postings_analyzed"] == 2


def test_skill_demand_reports_empty_without_profile(ctx):
    assert skill_demand_reports(ctx, propose=False) == []


# ---------------------------------------------------------------------------
# Learning-focus proposals
# ---------------------------------------------------------------------------

def test_propose_creates_pending_approval_only_after_human_approves(ctx):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer", skills=[])
    for i in range(4):
        _seed_posting(ctx, f"Flutter Developer - {i}", "Requires Docker experience.")

    report = skill_demand_report(ctx, "Flutter Developer", propose=True)
    proposed = {p["skill"]: p for p in report["proposed_focuses"]}
    assert proposed["Docker"]["status"] == "proposed"

    pending = ctx.store.list_approvals("pending")
    docker_approvals = [a for a in pending if a["payload"].get("args", {}).get("topic") == "Docker"]
    assert len(docker_approvals) == 1
    assert docker_approvals[0]["payload"]["tool"] == "create_learning_focus"
    assert docker_approvals[0]["tier"] == 2

    # not created yet — only the approval is parked. A direct query, not
    # list_focuses() — that helper auto-seeds a default focus for a
    # zero-row user (see career_scout.py's _propose_focuses_for_demand
    # docstring), which would make this assertion fail for the wrong reason.
    def _topics():
        return {r["topic"] for r in ctx.collab.conn.execute(
            "SELECT topic FROM learning_focuses WHERE uid=?", (ctx.user_id,)).fetchall()}
    assert _topics() == set()

    approve(ctx, docker_approvals[0]["id"])
    assert "Docker" in _topics()


def test_propose_does_not_duplicate_pending_approval_on_rerun(ctx):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer", skills=[])
    for i in range(4):
        _seed_posting(ctx, f"Flutter Developer - {i}", "Requires Docker experience.")

    skill_demand_report(ctx, "Flutter Developer", propose=True)
    skill_demand_report(ctx, "Flutter Developer", propose=True)

    pending = ctx.store.list_approvals("pending")
    docker_approvals = [a for a in pending if a["payload"].get("args", {}).get("topic") == "Docker"]
    assert len(docker_approvals) == 1


def test_propose_skips_skill_with_existing_focus(ctx):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer", skills=[])
    for i in range(4):
        _seed_posting(ctx, f"Flutter Developer - {i}", "Requires Docker experience.")
    add_focus(ctx.collab.conn, ctx.user_id, "Docker")

    report = skill_demand_report(ctx, "Flutter Developer", propose=True)
    proposed = {p["skill"]: p for p in report["proposed_focuses"]}
    assert proposed["Docker"]["status"] == "already_tracked"

    pending = ctx.store.list_approvals("pending")
    docker_approvals = [a for a in pending if a["payload"].get("args", {}).get("topic") == "Docker"]
    assert docker_approvals == []


def test_propose_via_registered_tool(ctx):
    from amy.tools.registry import invoke as tool_invoke

    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer", skills=[])
    for i in range(4):
        _seed_posting(ctx, f"Flutter Developer - {i}", "Requires Docker experience.")

    out = tool_invoke(ctx, "skill_demand_report", {"track": "Flutter Developer"}, actor="human")
    assert out["track"] == "Flutter Developer"
    assert any(p["skill"] == "Docker" for p in out["proposed_focuses"])
