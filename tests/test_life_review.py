"""LIFE AUTOPILOT L6 — monthly life review + briefing integration.
Idempotent-per-month + section coverage, per the spec's explicit test."""
import datetime as _dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.automation.closers import _life_section
from amy.collab import CollabDB
from amy.life import inference as life_inference
from amy.life import review as life_review


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-review", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def test_generate_month_creates_note_with_all_sections(ctx):
    # use the CURRENT month so a proposal created "now" (real timestamp)
    # falls inside the review's date window
    target_month = _dt.date.today().strftime("%Y-%m")
    start, _ = life_review._month_bounds(target_month)
    ctx.store.upsert_life_metrics(ctx.user_id, _dt.date.today().isoformat(), day_type="weekday",
                                  grace=False, office_minutes=480, signal_counts={})

    life_inference.propose(
        ctx, "meals", "cook_habit", "Cook at home more often?", "evidence",
        "propose_habit", {"title": "Cook dinner at home", "frequency": "daily"}, "reasoning")

    result = life_review.generate_month(ctx, month=target_month)
    assert result["month"] == target_month
    assert result["note"] != "already-written"
    note_path = Path(result["note"])
    assert note_path.exists()
    body = note_path.read_text(encoding="utf-8")
    assert "## Observed vs baselines" in body
    assert "## Suggested" in body
    assert "## Accepted" in body
    assert "## Rejected" in body
    assert "## Pruned" in body
    assert "Cook at home more often?" in body


def test_generate_month_idempotent(ctx):
    target_month = life_review.last_month()
    r1 = life_review.generate_month(ctx, month=target_month)
    assert r1["note"] != "already-written"
    r2 = life_review.generate_month(ctx, month=target_month)
    assert r2["note"] == "already-written"


def test_pruned_section_lists_silenced_rules(ctx):
    ctx.collab.conn.execute(
        "INSERT INTO prefs(key,value) VALUES(?,?)", ("life_opp_dismiss_grocery", "2"))
    ctx.collab.conn.execute(
        "INSERT INTO prefs(key,value) VALUES(?,?)", ("life_opp_dismiss_cadence", "1"))
    ctx.collab.conn.commit()
    result = life_review.generate_month(ctx, month=life_review.last_month())
    assert result["pruned"] == 1
    body = Path(result["note"]).read_text(encoding="utf-8")
    assert "grocery" in body
    assert "cadence" not in body.split("## Pruned")[1]


# ---------------------------------------------------------------------------
# life.pattern_detected emission (the L6 integration fix)
# ---------------------------------------------------------------------------

def test_propose_emits_pattern_detected_event(ctx):
    result = life_inference.propose(
        ctx, "commute", "leave_by", "Leave by 6?", "evidence",
        "propose_habit", {"title": "Leave office by 18:00", "frequency": "daily"}, "reasoning")
    assert result is not None
    row = ctx.collab.conn.execute(
        "SELECT payload FROM events WHERE type='life.pattern_detected'").fetchone()
    assert row is not None
    import json
    payload = json.loads(row["payload"])
    assert payload["agent"] == "commute"
    assert payload["pattern_key"] == "leave_by"
    assert "summary" in payload


# ---------------------------------------------------------------------------
# Briefing Life section
# ---------------------------------------------------------------------------

def test_life_section_reports_auto_checks_and_deadlines(ctx):
    today_s = _dt.date.today().isoformat()
    import uuid
    ctx.collab.conn.execute(
        "INSERT INTO events(id,ts,type,payload,source) VALUES(?,?,?,?,?)",
        (uuid.uuid4().hex, f"{today_s}T09:00:00", "life.habit_autocompleted", "{}", "habit_signals"))
    ctx.collab.conn.commit()

    fe = ctx.open_finance()
    try:
        from amy.commitments import CommitmentEngine
        due = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
        CommitmentEngine(fe).add("custom", "Renew passport", due)
    finally:
        fe.close()

    lines = _life_section(ctx)
    assert len(lines) == 1
    text = lines[0]
    assert text.startswith("Life: ")
    assert "auto-tracked today" in text
    assert "Renew passport" in text


def test_life_section_empty_when_nothing_to_report(ctx):
    lines = _life_section(ctx)
    assert lines == []


def test_life_section_disabled_by_master_switch(ctx, monkeypatch):
    monkeypatch.setenv("AMY_LIFE_AUTOPILOT", "0")
    today_s = _dt.date.today().isoformat()
    import uuid
    ctx.collab.conn.execute(
        "INSERT INTO events(id,ts,type,payload,source) VALUES(?,?,?,?,?)",
        (uuid.uuid4().hex, f"{today_s}T09:00:00", "life.habit_autocompleted", "{}", "habit_signals"))
    ctx.collab.conn.commit()
    assert _life_section(ctx) == []


def test_life_section_mentions_one_pattern_insight_max(ctx):
    life_inference.propose(
        ctx, "meals", "cook_habit", "Cook?", "evidence", "propose_habit",
        {"title": "Cook", "frequency": "daily"}, "reasoning")
    life_inference.propose(
        ctx, "sleep", "wind_down", "Wind down?", "evidence", "propose_habit",
        {"title": "Wind down", "frequency": "daily"}, "reasoning")
    lines = _life_section(ctx)
    assert len(lines) == 1
    # only the MOST RECENT pattern shows, not both
    assert lines[0].count("Pattern noticed:") <= 1
