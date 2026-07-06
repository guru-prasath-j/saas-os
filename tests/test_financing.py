"""Phase R7A-4 — financing models: total-cost math, comparison,
pack-driven enablement, values-profile flagging."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.financing import (FINANCING_MODELS, compare,
                           flagged_models_from_profiles, list_models)
from amy.jurisdictions import load_pack


def test_amortized_interest_math():
    # 100k over 12 months at 12%/yr → pmt ≈ 8884.88, total ≈ 106,618.55
    q = FINANCING_MODELS["amortized_interest"].quote(100000, 12, 0.12)
    assert q["monthly_payment"] == pytest.approx(8884.88, abs=0.02)
    assert q["total_cost"] == pytest.approx(106618.55, abs=0.5)
    assert q["cost_of_financing"] > 0


def test_markup_and_lease_math():
    q = FINANCING_MODELS["profit_rate_markup"].quote(100000, 12, 0.10)
    assert q["total_cost"] == 110000.0            # flat 10% for one year
    assert q["monthly_payment"] == pytest.approx(9166.67, abs=0.01)
    q2 = FINANCING_MODELS["lease_to_own"].quote(100000, 24, 0.10)
    assert q2["total_cost"] == 120000.0           # 10%/yr × 2 years


def test_zero_interest():
    q = FINANCING_MODELS["installment_zero_interest"].quote(100000, 10)
    assert q["total_cost"] == 100000.0
    assert q["monthly_payment"] == 10000.0
    assert q["cost_of_financing"] == 0.0


def test_compare_sorted_and_pack_driven():
    # UAE pack enables markup/lease/zero-interest — no amortized_interest
    uae = load_pack("uae")
    out = compare(100000, 12, 0.12, enabled_models=uae["financing_models"])
    assert [q["model"] for q in out][0] == "installment_zero_interest"
    assert "amortized_interest" not in {q["model"] for q in out}
    assert out == sorted(out, key=lambda q: q["total_cost"])
    # India pack includes amortized_interest
    india = load_pack("india")
    out2 = compare(100000, 12, 0.12, enabled_models=india["financing_models"])
    assert "amortized_interest" in {q["model"] for q in out2}


def test_values_profile_flags_interest_model():
    from amy.values import get_preset
    profiles = [{"name": "Interest-free finance",
                 "rules": get_preset("interest_free_finance")["rules"]}]
    flagged = flagged_models_from_profiles(profiles)
    assert "amortized_interest" in flagged
    out = compare(50000, 12, 0.12, flagged_models=flagged)
    amort = next(q for q in out if q["model"] == "amortized_interest")
    assert amort["flagged"] is True and "Interest-based" in amort["flag_reason"]
    zero = next(q for q in out if q["model"] == "installment_zero_interest")
    assert "flagged" not in zero


def test_new_model_is_one_class(monkeypatch):
    from amy import financing

    class BalloonPayment(financing.FinancingModel):
        name = "balloon_payment"
        description = "test model"

        def quote(self, principal, months, annual_rate=0.0):
            return self._base(principal, months, principal * 1.05)

    financing.register(BalloonPayment())
    try:
        assert "balloon_payment" in {m["name"] for m in list_models()}
        out = compare(1000, 10, enabled_models=["balloon_payment"])
        assert out[0]["total_cost"] == 1050.0
    finally:
        financing.FINANCING_MODELS.pop("balloon_payment", None)
