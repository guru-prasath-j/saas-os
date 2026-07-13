"""CAREER AUTOPILOT Phase C — Autonomous Career Sprint: Monday generation +
Sunday review, on top of Phase A (learning focuses) / Phase B (skill-gap
roadmap) data.

All postings/applications/profiles/focuses constructed here are SYNTHETIC
test fixtures, not real career data. See amy/career_sprint.py's module
docstring for the domain='career_sprint' (not 'career') reasoning and the
honesty rules each function follows.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.autonomous import GoalEngine
from amy.career_scout import _extract_posting_keywords
from amy.career_sprint import explain_progress, generate_sprint, review_sprint
from amy.collab import CollabDB


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-careersprint", "careersprint@example.com", cdb, tmp_path, llm_router=None)
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


def _seed_active_career_goal(ctx, target_role="Flutter Developer"):
    ctx.store.set_career_profile(ctx.user_id, target_role=target_role, skills=[])
    return GoalEngine(ctx.collab).create_goal(f"Become a {target_role}", domain="career")


# ---------------------------------------------------------------------------
# generate_sprint — honest skips, real skill-gap/focus/target data
# ---------------------------------------------------------------------------

def test_generate_sprint_skipped_without_active_career_goal(ctx):
    assert generate_sprint(ctx) == {"skipped": "no active career goal"}


def test_generate_sprint_skipped_without_target_role(ctx):
    GoalEngine(ctx.collab).create_goal("Vague career goal", domain="career")
    assert generate_sprint(ctx) == {"skipped": "no target_role on file"}


def test_generate_sprint_reflects_real_skill_gaps_and_creates_domain_career_sprint_goal(ctx):
    _seed_active_career_goal(ctx)
    for i in range(4):
        _seed_posting(ctx, f"Flutter Developer - {i}", "Acme",
                      "Requires Docker and AWS experience.")

    out = generate_sprint(ctx)
    assert out["status"] == "auto_executed"
    assert out["skill_gaps_total"] > 0
    assert out["skill_gaps_addressed"] > 0

    # the sprint goal must NOT be domain='career' — that would collide with
    # every existing "find the career goal" query (job_scout_poll,
    # portfolio_review, career_goal_stall_check, _active_career_goal) which
    # all pick the most-recently-created domain='career' active row
    sprint_rows = ctx.collab.conn.execute(
        "SELECT * FROM goals WHERE domain='career_sprint'").fetchall()
    assert len(sprint_rows) == 1
    career_rows = ctx.collab.conn.execute(
        "SELECT * FROM goals WHERE domain='career'").fetchall()
    assert len(career_rows) == 1   # untouched — still just the original goal

    milestones = ctx.collab.conn.execute(
        "SELECT title FROM milestones WHERE goal_id=?", (sprint_rows[0]["id"],)).fetchall()
    titles = " ".join(m["title"] for m in milestones).lower()
    assert "docker" in titles or "aws" in titles


def test_generate_sprint_is_tier_1_auto_executed_not_pending(ctx):
    _seed_active_career_goal(ctx)
    _seed_posting(ctx, "Flutter Developer - A", "Acme", "Requires Docker.")
    out = generate_sprint(ctx)
    # tier 1 (submit_action) executes immediately and notifies — never parks
    # a pending approval the way tier 2 would
    assert out["status"] == "auto_executed"


def test_generate_sprint_dedups_within_same_iso_week(ctx):
    _seed_active_career_goal(ctx)
    _seed_posting(ctx, "Flutter Developer - A", "Acme", "Requires Docker.")
    generate_sprint(ctx)
    second = generate_sprint(ctx)
    assert second["status"] == "duplicate"
    rows = ctx.collab.conn.execute(
        "SELECT COUNT(*) c FROM goals WHERE domain='career_sprint'").fetchone()
    assert rows["c"] == 1


def test_application_target_null_with_reason_when_no_history(ctx):
    _seed_active_career_goal(ctx)
    _seed_posting(ctx, "Flutter Developer - A", "Acme", "Requires Docker.")
    out = generate_sprint(ctx)
    assert out["application_target"]["target"] is None
    assert "reason" in out["application_target"]


def test_application_target_real_trailing_average_when_history_exists(ctx):
    _seed_active_career_goal(ctx)
    pid = _seed_posting(ctx, "Flutter Developer - A", "Acme", "Requires Docker.")
    for _ in range(4):
        ctx.store.create_application(ctx.user_id, pid, channel="email")
    out = generate_sprint(ctx)
    target = out["application_target"]
    assert target["target"] is not None
    assert target["target"] >= 1
    assert "basis" in target


def test_generate_sprint_no_fabricated_score_language(ctx):
    _seed_active_career_goal(ctx)
    _seed_posting(ctx, "Flutter Developer - A", "Acme", "Requires Docker.")
    out = generate_sprint(ctx)
    blob = str(out).lower()
    assert "profile score" not in blob
    assert "market readiness" not in blob


# ---------------------------------------------------------------------------
# review_sprint — real counts, honest skills-added unavailability
# ---------------------------------------------------------------------------

def test_review_sprint_skipped_without_a_sprint(ctx):
    assert review_sprint(ctx) == {"skipped": "no sprint goal on file"}


def test_review_sprint_reflects_real_task_and_application_counts(ctx):
    _seed_active_career_goal(ctx)
    pid = _seed_posting(ctx, "Flutter Developer - A", "Acme", "Requires Docker.")
    generate_sprint(ctx)

    sprint_row = ctx.collab.conn.execute(
        "SELECT id FROM goals WHERE domain='career_sprint'").fetchone()
    engine = GoalEngine(ctx.collab)
    task_ids = [t["id"] for t in engine.list_tasks(sprint_row["id"])]
    assert task_ids
    engine.complete_task(task_ids[0], done=True)

    aid = ctx.store.create_application(ctx.user_id, pid, channel="email")
    ctx.store.update_application_status(ctx.user_id, aid, "interview", "phone screen")

    out = review_sprint(ctx)
    assert out["tasks_completed"] == 1
    assert out["tasks_planned"] == len(task_ids)
    assert out["applications_sent"] == 1
    assert out["interviews_scheduled"] == 1
    assert out["skills_added"] == {"available": False,
                                   "reason": "career_profile.skills has no "
                                             "historical snapshot to diff against"}
    assert out["note"] != "already-written"
    assert Path(out["note"]).exists()


def test_review_sprint_idempotent_per_week(ctx):
    _seed_active_career_goal(ctx)
    _seed_posting(ctx, "Flutter Developer - A", "Acme", "Requires Docker.")
    generate_sprint(ctx)
    first = review_sprint(ctx)
    second = review_sprint(ctx)
    assert second["note"] == "already-written"
    assert first["week"] == second["week"]


# ---------------------------------------------------------------------------
# explain_progress — assistant tool support
# ---------------------------------------------------------------------------

def test_explain_progress_unavailable_without_a_sprint(ctx):
    out = explain_progress(ctx)
    assert out["available"] is False


def test_explain_progress_reflects_real_task_completion(ctx):
    _seed_active_career_goal(ctx)
    _seed_posting(ctx, "Flutter Developer - A", "Acme", "Requires Docker.")
    generate_sprint(ctx)

    sprint_row = ctx.collab.conn.execute(
        "SELECT id FROM goals WHERE domain='career_sprint'").fetchone()
    engine = GoalEngine(ctx.collab)
    task_ids = [t["id"] for t in engine.list_tasks(sprint_row["id"])]
    engine.complete_task(task_ids[0], done=True)

    out = explain_progress(ctx)
    assert out["available"] is True
    assert out["tasks_completed"] == 1
    assert out["tasks_planned"] == len(task_ids)
    assert out["days_remaining"] is not None
