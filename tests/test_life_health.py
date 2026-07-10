"""LIFE AUTOPILOT L1 — health bootstrap + targets.

Bootstrap present -> correct Mifflin-St Jeor math + sensitive=True routing;
missing -> dormancy + exactly one notification listing what's needed;
vault edit -> tier-1 re-parse with a diff; >5% weight shift -> tier-2
re-proposal with the delta shown; forbidden-phrase assertion on every
generated template string (advisory-never-diagnostic hard rule).
All LLM calls mocked — no live network/Ollama calls in tests.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.collab import CollabDB
from amy.life import targets as life_targets
from amy.life.bootstrap import (bootstrap_health_profile, check_vault_reparse,
                                check_weight_shift, find_health_folder,
                                missing_essentials, propose_health_targets)

# Same forbidden-phrase list the spec requires be asserted against every
# generated line template (advisory, never diagnostic).
FORBIDDEN_PHRASES = [
    "you are stressed", "you're stressed", "you seem stressed",
    "you are burned out", "you're burned out", "burnout",
    "you are depressed", "you're depressed",
    "you are anxious", "you're anxious",
    "you have a mental health", "you need therapy", "you should see a doctor",
    "you are unwell", "diagnosis", "diagnosed with",
]


def _assert_no_forbidden_phrases(*texts: str) -> None:
    for text in texts:
        low = (text or "").lower()
        for phrase in FORBIDDEN_PHRASES:
            assert phrase not in low, f"forbidden phrase {phrase!r} found in: {text!r}"


class StubLLM:
    def __init__(self, response: dict | str):
        self._response = response
        self.calls: list[dict] = []

    def generate(self, system, prompt, context="", sensitive=False, fast=False):
        self.calls.append({"system": system, "prompt": prompt, "sensitive": sensitive})
        body = self._response if isinstance(self._response, str) else json.dumps(self._response)
        return (body, "scripted")


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-health", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _vault_dir(ctx) -> Path:
    from amy.saas import tenancy
    d = tenancy.resolve_vault_dir(ctx.user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


_PROFILE_NOTE = (
    "# Health\n\ndob_or_age: 34\nsex: male\nheight_cm: 178\nweight_kg: 80\n"
    "activity_level: moderate\nconstraints: none\n"
)
_PARSED_RESPONSE = {
    "dob_or_age": "34", "sex": "male", "height_cm": 178, "weight_kg": 80,
    "activity_level": "moderate", "constraints": "none",
}


def test_no_health_folder_stays_dormant(ctx):
    vault = _vault_dir(ctx)
    (vault / "00_Daily").mkdir()
    result = bootstrap_health_profile(ctx)
    assert result["status"] == "dormant"
    assert result["reason"] == "no_folder"
    notes = ctx.collab.conn.execute(
        "SELECT * FROM notifications WHERE type='health_bootstrap_needed'").fetchall()
    assert len(notes) == 1
    _assert_no_forbidden_phrases(notes[0]["body"], notes[0]["title"])


def test_folder_found_but_missing_essentials_notifies_exact_list(ctx):
    vault = _vault_dir(ctx)
    (vault / "Health").mkdir()
    (vault / "Health" / "notes.md").write_text("just some prose, no numbers", encoding="utf-8")
    ctx.llm = StubLLM({"dob_or_age": "34", "sex": None, "height_cm": None,
                       "weight_kg": None, "activity_level": None, "constraints": ""})
    result = bootstrap_health_profile(ctx)
    assert result["status"] == "dormant"
    assert result["reason"] == "missing_essentials"
    assert "sex (for the BMR formula)" in result["missing"]
    assert "weight (kg)" in result["missing"]
    notes = ctx.collab.conn.execute(
        "SELECT * FROM notifications WHERE type='health_bootstrap_needed'").fetchall()
    assert len(notes) == 1
    assert "sex" in notes[0]["body"]
    _assert_no_forbidden_phrases(notes[0]["body"])
    # a single call, sensitive=True (privacy floor)
    assert len(ctx.llm.calls) == 1
    assert ctx.llm.calls[0]["sensitive"] is True


def test_full_profile_computes_correct_math_and_proposes_targets(ctx):
    vault = _vault_dir(ctx)
    (vault / "Health").mkdir()
    (vault / "Health" / "profile.md").write_text(_PROFILE_NOTE, encoding="utf-8")
    ctx.llm = StubLLM(_PARSED_RESPONSE)

    result = bootstrap_health_profile(ctx)
    assert result["status"] == "bootstrapped"
    assert all(s == "pending" for s in result["proposals"])

    # sensitive routing asserted
    assert ctx.llm.calls[0]["sensitive"] is True

    # correct Mifflin-St Jeor math: 10*80 + 6.25*178 - 5*34 + 5 = 800+1112.5-170+5 = 1747.5
    bmr = life_targets.bmr_mifflin_st_jeor("male", 80.0, 178.0, 34)
    assert bmr["value"] == pytest.approx(1747.5)
    tdee = life_targets.tdee(bmr["value"], "moderate")
    assert tdee["value"] == pytest.approx(1747.5 * 1.55, abs=0.1)

    approvals = ctx.collab.conn.execute(
        "SELECT title, body, tier, status FROM approvals"
        " WHERE action_type='health_target_propose'").fetchall()
    assert len(approvals) == 4   # calorie, sleep, protein, water
    kinds = {a["title"] for a in approvals}
    assert any("calorie" in k.lower() for k in kinds)
    assert any("sleep" in k.lower() for k in kinds)
    for a in approvals:
        assert a["tier"] == 2
        assert a["status"] == "pending"
        assert "formula" in a["body"].lower() or "×" in a["body"] or "kcal" in a["body"].lower()
    _assert_no_forbidden_phrases(*(a["body"] for a in approvals), *(a["title"] for a in approvals))

    # re-running is a no-op (already_bootstrapped) — no duplicate proposals
    result2 = bootstrap_health_profile(ctx)
    assert result2["status"] == "already_bootstrapped"
    approvals2 = ctx.collab.conn.execute(
        "SELECT id FROM approvals WHERE action_type='health_target_propose'").fetchall()
    assert len(approvals2) == 4


def test_find_health_folder_fuzzy_matches_variant_names(tmp_path):
    (tmp_path / "Fitness Tracking").mkdir()
    (tmp_path / "00_Daily").mkdir()
    found = find_health_folder(tmp_path)
    assert found is not None
    assert found.name == "Fitness Tracking"


def test_find_health_folder_none_when_nothing_matches(tmp_path):
    (tmp_path / "00_Daily").mkdir()
    (tmp_path / "08_Captures").mkdir()
    assert find_health_folder(tmp_path) is None


def test_vault_edit_triggers_tier1_reparse_with_diff(ctx, monkeypatch):
    import time

    vault = _vault_dir(ctx)
    folder = vault / "Health"
    folder.mkdir()
    note = folder / "profile.md"
    note.write_text(_PROFILE_NOTE, encoding="utf-8")
    ctx.llm = StubLLM(_PARSED_RESPONSE)
    result = bootstrap_health_profile(ctx)
    assert result["status"] == "bootstrapped"

    # edit the note — activity level changes, mtime must move forward
    time.sleep(0.05)
    note.write_text(_PROFILE_NOTE.replace("moderate", "active"), encoding="utf-8")
    import os
    os.utime(note, None)

    ctx.llm = StubLLM({**_PARSED_RESPONSE, "activity_level": "active"})
    out = check_vault_reparse(ctx)
    assert out is not None
    assert out["status"] in ("auto_executed", "failed")
    approvals = ctx.collab.conn.execute(
        "SELECT tier, body FROM approvals WHERE action_type='health_target_propose'"
        " AND body LIKE '%activity_level%'").fetchall()
    assert len(approvals) == 1
    assert approvals[0]["tier"] == 1
    assert "moderate" in approvals[0]["body"] and "active" in approvals[0]["body"]
    _assert_no_forbidden_phrases(approvals[0]["body"])


def test_weight_shift_over_5pct_reproposes_tier2_with_delta(ctx):
    vault = _vault_dir(ctx)
    (vault / "Health").mkdir()
    (vault / "Health" / "profile.md").write_text(_PROFILE_NOTE, encoding="utf-8")
    ctx.llm = StubLLM(_PARSED_RESPONSE)
    bootstrap_health_profile(ctx)

    shift = ctx.store.append_weight_log(ctx.user_id, 90.0)  # 80 -> 90 = +12.5%
    assert shift["pct_change"] == pytest.approx(12.5)
    out = check_weight_shift(ctx, shift)
    assert out is not None
    assert out["status"] == "pending"
    approvals = ctx.collab.conn.execute(
        "SELECT tier, body FROM approvals WHERE action_type='health_target_propose'"
        " AND body LIKE '%shifted%' OR body LIKE '%80.0kg to 90.0kg%'").fetchall()
    row = ctx.collab.conn.execute(
        "SELECT tier, body FROM approvals WHERE action_type='health_target_propose'"
        " AND payload LIKE '%weight_shift_recompute%'").fetchone()
    assert row is not None
    assert row["tier"] == 2
    assert "12.5" in row["body"]
    _assert_no_forbidden_phrases(row["body"])


def test_weight_shift_under_5pct_no_reproposal(ctx):
    vault = _vault_dir(ctx)
    (vault / "Health").mkdir()
    (vault / "Health" / "profile.md").write_text(_PROFILE_NOTE, encoding="utf-8")
    ctx.llm = StubLLM(_PARSED_RESPONSE)
    bootstrap_health_profile(ctx)

    shift = ctx.store.append_weight_log(ctx.user_id, 82.0)  # 80 -> 82 = +2.5%
    out = check_weight_shift(ctx, shift)
    assert out is None
    row = ctx.collab.conn.execute(
        "SELECT id FROM approvals WHERE action_type='health_target_propose'"
        " AND payload LIKE '%weight_shift_recompute%'").fetchone()
    assert row is None


def test_missing_essentials_helper():
    assert missing_essentials({}) == [
        "date of birth or age", "sex (for the BMR formula)", "height (cm)",
        "weight (kg)", "activity level (sedentary/light/moderate/active/very_active)",
    ]
    complete = {"dob_or_age": "30", "sex": "female", "height_cm": 165,
               "weight_kg": 60, "activity_level": "light"}
    assert missing_essentials(complete) == []


def test_resolve_age_dob_and_plain_age():
    assert life_targets.resolve_age("30") == 30
    assert life_targets.resolve_age("not a date") is None
    assert life_targets.resolve_age("") is None


def test_all_targets_disclaimer_present_and_forbidden_phrases_absent():
    out = life_targets.all_targets("female", 60.0, 165.0, 28, "light")
    assert "estimate" in out["disclaimer"].lower()
    assert "not medical advice" in out["disclaimer"].lower()
    _assert_no_forbidden_phrases(out["disclaimer"], out["bmr"]["formula"],
                                 out["tdee"]["formula"], out["sleep"]["formula"],
                                 out["protein"]["formula"], out["water"]["formula"])
