"""Phase R7B — jurisdiction packs, effective-date versioning, deadlines,
FX conversion, locale formatting."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.jurisdictions import (LOAN_TYPES, PackError, list_packs, load_pack,
                               list_obligation_presets, loan_config,
                               obligation_preset, resolve_version,
                               upcoming_deadlines, validate_loan_config,
                               validate_pack)
from amy.fx import FxConverter
from amy.locale_fmt import format_money, group_number, prompt_hint


# --- packs -------------------------------------------------------------------

def test_three_packs_ship_and_validate():
    ids = {p["id"] for p in list_packs()}
    assert {"uae", "us", "india"} <= ids
    for pid in ("uae", "us", "india"):
        pack = load_pack(pid)          # raises PackError if invalid
        assert pack["disclaimer"]
        assert pack["currency"]["code"]


def test_pack_specifics():
    india = load_pack("india")
    assert india["currency"]["grouping"] == "indian"
    assert india["fiscal_year"]["start_month"] == 4
    uae = load_pack("uae")
    assert "hijri" in uae["calendar_systems"]
    assert "zakat" in {p["id"] for p in uae["obligation_presets"]}
    ct = obligation_preset(uae, "corporate_tax_estimate")
    assert ct["rate"] == 0.09 and ct["wealth_threshold"]["amount"] == 375000
    us = load_pack("us")
    q = obligation_preset(us, "quarterly_tax_estimate")
    assert [s["month"] for s in q["schedule"]] == [4, 6, 9, 1]


def test_effective_date_versioning():
    us = load_pack("us")
    r24 = obligation_preset(us, "retirement_contribution", dt.date(2024, 6, 1))
    r25 = obligation_preset(us, "retirement_contribution", dt.date(2025, 6, 1))
    assert r24["wealth_threshold"]["amount"] == 23000
    assert r25["wealth_threshold"]["amount"] == 23500
    # a rate change is a data edit, never a code change
    assert resolve_version([{"effective_from": "2020-01-01",
                             "effective_to": "2020-12-31", "rate": 1}],
                           dt.date(2021, 5, 1)) is None


def test_presets_carry_disclaimer_and_jurisdiction():
    for pid in ("uae", "us", "india"):
        for p in list_obligation_presets(load_pack(pid)):
            assert p["jurisdiction"] == pid
            assert "ESTIMATES" in p["disclaimer"]


def test_upcoming_deadlines_india_advance_tax():
    india = load_pack("india")
    dls = upcoming_deadlines(india, after=dt.date(2026, 7, 6), horizon_days=90)
    names = [d["name"] for d in dls]
    assert any("2nd installment" in n for n in names)         # Sep 15
    assert any("GSTR-3B" in n for n in names)                 # monthly 20th
    assert all(d["days_away"] > 0 for d in dls)
    assert dls == sorted(dls, key=lambda d: d["date"])


def test_upcoming_deadlines_us_and_uae():
    us_dls = upcoming_deadlines(load_pack("us"), after=dt.date(2026, 3, 20),
                                horizon_days=40)
    assert any(d["name"].startswith("Federal income-tax") for d in us_dls)
    uae_dls = upcoming_deadlines(load_pack("uae"), after=dt.date(2026, 7, 6),
                                 horizon_days=40)
    assert any("VAT" in d["name"] for d in uae_dls)


def test_fourth_jurisdiction_is_json_only(tmp_path):
    """The docs template validates — proving pack #4 needs no code."""
    template = {
        "id": "example", "name": "Example Country",
        "currency": {"code": "EXC", "symbol": "E", "grouping": "western",
                     "decimals": 2},
        "fiscal_year": {"calendar": "fiscal", "start_month": 1},
        "calendar_systems": ["gregorian"],
        "financing_models": ["amortized_interest"],
        "default_screening_profiles": ["budget_discipline"],
        "obligation_presets": [{
            "id": "x_tax", "name": "X", "kind": "scheduled_estimate",
            "calendar_system": "gregorian",
            "versions": [{"effective_from": "2025-01-01", "effective_to": None,
                          "schedule": [{"month": 4, "day": 15,
                                        "cumulative_portion": 1.0}]}]}],
        "compliance_deadlines": [{
            "id": "filing", "name": "Filing", "calendar_system": "gregorian",
            "versions": [{"effective_from": "2025-01-01", "effective_to": None,
                          "month": 4, "day": 30}]}],
        "disclaimer": "Estimates only.",
    }
    assert validate_pack(template) == []
    # and a broken pack reports its problems instead of half-loading
    bad = dict(template)
    del bad["disclaimer"]
    assert any("disclaimer" in p for p in validate_pack(bad))


# --- Phase 4: loan_config extension --------------------------------------------

def test_all_three_packs_have_a_valid_loan_config():
    for pid in ("india", "uae", "us"):
        pack = load_pack(pid)          # still loads/validates after the extension
        cfg = loan_config(pack)
        assert cfg is not None
        assert validate_loan_config(pack) == []
        assert cfg["_disclaimer"]
        for lt in LOAN_TYPES:
            entry = cfg["loan_limits"][lt]
            assert entry["amount"] > 0
            assert entry["currency"]
        assert 0 < cfg["max_debt_to_income_ratio"] <= 1
        assert cfg["late_fee_rules"]["type"] in ("percentage_of_overdue", "flat_fee")
        assert isinstance(cfg["late_fee_rules"]["grace_days"], int)


def test_islamic_finance_only_true_for_uae():
    assert loan_config(load_pack("uae"))["islamic_finance_available"] is True
    assert loan_config(load_pack("india"))["islamic_finance_available"] is False
    assert loan_config(load_pack("us"))["islamic_finance_available"] is False


def test_late_fee_rules_shape_matches_type():
    india_fee = loan_config(load_pack("india"))["late_fee_rules"]
    assert india_fee["type"] == "percentage_of_overdue"
    assert isinstance(india_fee["rate"], (int, float))
    us_fee = loan_config(load_pack("us"))["late_fee_rules"]
    assert us_fee["type"] == "flat_fee"
    assert isinstance(us_fee["amount"], (int, float))


def test_loan_config_is_optional_but_validated_when_present():
    # a pack with no loan_config at all is a valid state, not an error —
    # loan_config() returns None rather than raising or fabricating one
    template = {"id": "x"}
    assert loan_config(template) is None

    # but a PRESENT, broken loan_config is caught, not half-accepted
    pack = dict(load_pack("india"))
    broken = dict(pack["loan_config"])
    broken["loan_limits"] = dict(broken["loan_limits"])
    del broken["loan_limits"]["home"]
    del broken["max_debt_to_income_ratio"]
    pack = {**pack, "loan_config": broken}
    problems = validate_loan_config(pack)
    assert any("home" in p for p in problems)
    assert any("max_debt_to_income_ratio" in p for p in problems)
    # and validate_pack() (the one load_pack() actually calls) surfaces the same
    assert any("loan_config" in p for p in validate_pack(pack))


# --- FX -----------------------------------------------------------------------

def test_fx_seed_and_cross_rates(tmp_path):
    fx = FxConverter(cache_dir=tmp_path)
    assert fx.rate("USD", "USD") == 1.0
    inr_per_aed = fx.rate("AED", "INR")
    assert 20 < inr_per_aed < 30                    # ≈ 87 / 3.6725 ≈ 23.7
    assert fx.convert(100, "AED", "INR") == pytest.approx(100 * inr_per_aed, abs=0.01)
    with pytest.raises(ValueError, match="no FX rate"):
        fx.rate("XYZ", "INR")


def test_fx_pluggable_source_and_daily_cache(tmp_path):
    calls = {"n": 0}

    def source():
        calls["n"] += 1
        return {"INR": 80.0, "AED": 4.0}

    fx = FxConverter(source=source, cache_dir=tmp_path)
    assert fx.rate("AED", "INR") == 20.0
    fx.rate("INR", "AED")
    assert calls["n"] == 1                          # cached after first fetch
    # a NEW converter instance reads the day-cache file, not the source
    fx2 = FxConverter(source=source, cache_dir=tmp_path)
    assert fx2.rate("AED", "INR") == 20.0
    assert calls["n"] == 1
    # broken live source degrades to seed, never raises
    fx3 = FxConverter(source=lambda: 1 / 0, cache_dir=tmp_path / "other")
    assert fx3.rate("USD", "INR") > 1


# --- locale --------------------------------------------------------------------

def test_indian_vs_western_grouping():
    assert group_number(12345678.9, "indian", 2) == "1,23,45,678.90"
    assert group_number(12345678.9, "western", 2) == "12,345,678.90"
    assert group_number(999, "indian", 0) == "999"
    assert group_number(-1234567, "indian", 0) == "-12,34,567"


def test_format_money_from_pack_currency():
    india = load_pack("india")["currency"]
    uae = load_pack("uae")["currency"]
    assert format_money(1520000, india, decimals=0) == "₹15,20,000"
    assert format_money(66970.5, uae) == "AED 66,970.50"
    assert format_money(500, load_pack("us")["currency"], decimals=0) == "$500"


def test_prompt_hint():
    h = prompt_hint(load_pack("india")["currency"], "en-IN")
    assert "INR" in h and "lakh/crore" in h and "en-IN" in h
