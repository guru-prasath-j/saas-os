"""CAREER AUTOPILOT Phase D — Resume Version Manager: track-specific
resume drafts, the course-completion trigger, and version-performance
tracking.

All profiles/postings/applications/learning-items constructed here are
SYNTHETIC test fixtures, not real career data. See amy/career_resume.py's
module docstring for why this is additive to (not a replacement of) Part
5E's existing master-resume evolution, and the always-tier-2 rule.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.career_portfolio import persist_classification
from amy.career_resume import (
    generate_resume_version, propose_course_completion_bullet,
    resume_performance, scan_course_completions,
)
from amy.career_scout import _extract_posting_keywords
from amy.collab import CollabDB


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    # generate_resume_version's optional LLM narrative pass otherwise
    # builds a REAL LLMRouter and attempts real provider calls (quirk 24)
    # — none of these tests care about LLM output, only the deterministic
    # fallback draft's real-data guarantees.
    monkeypatch.setattr("amy.agents.reactive._get_llm", lambda ctx: None)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-resume", "resume@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _repo(name, matched=None):
    return {"name": name, "_matched_keywords": matched or [], "_missing": []}


def _seed_posting(ctx, title, company, description):
    posting = {"title": title, "company": company, "url": f"https://x/{title}-{company}",
              "location": "Remote", "salary": "", "is_remote": True,
              "description": description, "source": "jobspy",
              "keywords": _extract_posting_keywords(title, description)}
    pid, _ = ctx.store.add_posting_if_new(ctx.user_id, posting)
    return pid


# ---------------------------------------------------------------------------
# generate_resume_version — honest skip, real-only content, always tier 2
# ---------------------------------------------------------------------------

def test_generate_resume_version_skipped_without_master_resume(ctx):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer",
                                 skills=["Dart"])
    out = generate_resume_version(ctx, "Flutter Developer")
    assert out["skipped"] == "no master resume on file — set one via set_career_profile first"


def test_generate_resume_version_always_pending(ctx):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer",
                                 skills=["Dart", "Docker"],
                                 resume_text="Experienced developer.")
    out = generate_resume_version(ctx, "Flutter Developer")
    assert out["status"] == "pending"
    # never auto-saved — no resume_versions row exists yet
    assert ctx.store.list_resume_versions(ctx.user_id) == []


def test_generate_resume_version_never_invents_a_skill(ctx):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer",
                                 skills=["Dart"], resume_text="Experienced developer.")
    for i in range(3):
        _seed_posting(ctx, f"Flutter Developer - {i}", "Acme",
                      "Requires Docker and Kubernetes experience.")
    generate_resume_version(ctx, "Flutter Developer")
    # the draft only lives in the pending approval's payload — never
    # auto-applied anywhere else
    approvals = ctx.collab.conn.execute(
        "SELECT payload FROM approvals WHERE action_type='resume_version_create'"
        " ORDER BY created_at DESC LIMIT 1").fetchone()
    import json as _json
    payload = _json.loads(approvals["payload"])
    content = payload["content"]
    assert "Docker" not in content   # a real skill gap, but NOT on career_profile.skills
    assert "Kubernetes" not in content
    assert "Dart" in content         # the one real owned skill


def test_generate_resume_version_uses_real_showcase_bullets(ctx):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer",
                                 skills=["Dart"], resume_text="Experienced developer.")
    persist_classification(ctx, "Flutter Developer",
                           [_repo("acme-app", matched=["dart"])], [], [],
                           entries_by_repo={"acme-app": {
                               "why": "Real Dart project.",
                               "bullets": ["Built a real Dart mobile app"]}})
    generate_resume_version(ctx, "Flutter Developer")
    approvals = ctx.collab.conn.execute(
        "SELECT payload FROM approvals WHERE action_type='resume_version_create'"
        " ORDER BY created_at DESC LIMIT 1").fetchone()
    import json as _json
    content = _json.loads(approvals["payload"])["content"]
    assert "Built a real Dart mobile app" in content


# ---------------------------------------------------------------------------
# Course-completion trigger — reuses the existing resume_update executor
# ---------------------------------------------------------------------------

def test_propose_course_completion_bullet_none_without_master_resume(ctx):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer", skills=[])
    out = propose_course_completion_bullet(ctx, "Docker Fundamentals", "docker")
    assert out is None


def test_propose_course_completion_bullet_uses_resume_update_action(ctx):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer",
                                 skills=[], resume_text="Experienced developer.")
    out = propose_course_completion_bullet(ctx, "Docker Fundamentals", "docker")
    assert out["status"] == "pending"
    row = ctx.collab.conn.execute(
        "SELECT action_type FROM approvals WHERE status='pending'"
        " ORDER BY created_at DESC LIMIT 1").fetchone()
    assert row["action_type"] == "resume_update"   # reused, not a new executor


def test_scan_course_completions_only_fires_on_current_gap(ctx):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer",
                                 skills=[], resume_text="Experienced developer.")
    for i in range(3):
        _seed_posting(ctx, f"Flutter Developer - {i}", "Acme", "Requires Docker experience.")

    # a completion matching the current gap ("docker")
    ctx.collab.conn.execute(
        "INSERT INTO learning_feed_items(id,uid,source,title,focus_tag,saved,"
        " fetched_at,completed_at) VALUES('item1',?,'hn','Docker Basics','docker',0,?,?)",
        (ctx.user_id, "2026-01-01T00:00:00", "2026-01-02T00:00:00"))
    # a completion NOT matching any current gap
    ctx.collab.conn.execute(
        "INSERT INTO learning_feed_items(id,uid,source,title,focus_tag,saved,"
        " fetched_at,completed_at) VALUES('item2',?,'hn','Unrelated Topic','yoga',0,?,?)",
        (ctx.user_id, "2026-01-01T00:00:00", "2026-01-02T00:01:00"))
    ctx.collab.conn.commit()

    out = scan_course_completions(ctx)
    assert out["scanned"] == 2
    assert out["proposed"] == 1

    # a second scan (cursor advanced) proposes nothing more
    out2 = scan_course_completions(ctx)
    assert out2["scanned"] == 0
    assert out2["proposed"] == 0


# ---------------------------------------------------------------------------
# resume_performance — honest confidence marking
# ---------------------------------------------------------------------------

def test_resume_performance_insufficient_data_under_three(ctx):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer",
                                 skills=[], resume_text="Experienced developer.")
    vid = ctx.store.create_resume_version(ctx.user_id, "v1", "content", "Flutter Developer")
    pid = _seed_posting(ctx, "Flutter Developer - A", "Acme", "Requires Docker.")
    ctx.store.create_application(ctx.user_id, pid, channel="email", resume_version_id=vid)

    out = resume_performance(ctx)
    entry = out["versions"][0]
    assert entry["applications_count"] == 1
    assert entry["confidence"] == "insufficient_data"
    assert "interview_rate_pct" not in entry


def test_resume_performance_real_rate_at_threshold(ctx):
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer",
                                 skills=[], resume_text="Experienced developer.")
    vid = ctx.store.create_resume_version(ctx.user_id, "v1", "content", "Flutter Developer")
    for i in range(3):
        pid = _seed_posting(ctx, f"Flutter Developer - {i}", "Acme", "Requires Docker.")
        aid = ctx.store.create_application(ctx.user_id, pid, channel="email",
                                           resume_version_id=vid)
        if i == 0:
            ctx.store.update_application_status(ctx.user_id, aid, "interview", "screen")

    out = resume_performance(ctx)
    entry = out["versions"][0]
    assert entry["applications_count"] == 3
    assert entry["interviews_count"] == 1
    assert entry["confidence"] == "observed"
    assert entry["interview_rate_pct"] == pytest.approx(33.3, abs=0.1)


def test_resume_performance_lists_unused_version_with_zero_counts(ctx):
    ctx.store.create_resume_version(ctx.user_id, "unused", "content", "Flutter Developer")
    out = resume_performance(ctx)
    entry = out["versions"][0]
    assert entry["applications_count"] == 0
    assert entry["confidence"] == "insufficient_data"


# ---------------------------------------------------------------------------
# resume_version_id attach-after-the-fact
# ---------------------------------------------------------------------------

def test_set_application_resume_version_roundtrips(ctx):
    vid = ctx.store.create_resume_version(ctx.user_id, "v1", "content", "Flutter Developer")
    pid = _seed_posting(ctx, "Flutter Developer - A", "Acme", "Requires Docker.")
    aid = ctx.store.create_application(ctx.user_id, pid, channel="email")
    assert ctx.store.set_application_resume_version(ctx.user_id, aid, vid) is True
    app = ctx.store.get_application(ctx.user_id, aid)
    assert app["resume_version_id"] == vid
