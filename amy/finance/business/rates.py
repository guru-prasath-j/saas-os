"""Maintained rate table for GST slabs / depreciation blocks — the Compliance
pipeline (compliance.py) must read rates from here, never recall them from
LLM training data. Seeded once with a small starter set; refreshed later via
FinanceEngine.update_rate() (PATCH /api/business/rates/{id}), not an
automated fetcher.
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..engine import FinanceEngine

_EFFECTIVE_FROM = "2017-07-01"  # GST rollout date; illustrative starter set

# (rate_type, key, value dict, source_note)
_DEFAULT_RATES = [
    ("gst", "exempt_0pct", {"rate": 0}, "GST Act — nil-rated / exempt supplies"),
    ("gst", "goods_5pct", {"rate": 5}, "GST Act — Schedule I"),
    ("gst", "goods_12pct", {"rate": 12}, "GST Act — Schedule II"),
    ("gst", "services_18pct", {"rate": 18}, "GST Act — Schedule III (default services rate)"),
    ("gst", "luxury_28pct", {"rate": 28}, "GST Act — Schedule IV"),
    ("depreciation_block", "plant_machinery", {"block": 15},
     "Income Tax Act, Appendix I — general plant & machinery"),
    ("depreciation_block", "furniture_fittings", {"block": 10},
     "Income Tax Act, Appendix I — furniture & fittings"),
    ("depreciation_block", "computers_software", {"block": 40},
     "Income Tax Act, Appendix I — computers incl. software"),
    ("depreciation_block", "motor_vehicles", {"block": 15},
     "Income Tax Act, Appendix I — motor vehicles (non-commercial use)"),
    ("threshold", "presumptive_44ad_turnover", {"amount": 30000000},
     "Income Tax Act, Sec 44AD — presumptive taxation turnover limit"),
    ("threshold", "gst_registration_services", {"amount": 2000000},
     "GST Act — registration threshold for service providers (most states)"),
]


def seed_defaults(fe: "FinanceEngine") -> int:
    """Insert the starter rate set — idempotent, only inserts missing keys.
    Called once from FinanceEngine._migrate()."""
    inserted = 0
    for rate_type, key, value, note in _DEFAULT_RATES:
        if fe.rate_exists(rate_type, key):
            continue
        fe.add_rate(rate_type, key, json.dumps(value), _EFFECTIVE_FROM, source_note=note)
        inserted += 1
    return inserted


def lookup(fe: "FinanceEngine", rate_type: str | None = None) -> list[dict]:
    """Current (effective_to IS NULL) rate rows, with value parsed from JSON."""
    rows = fe.list_rates(rate_type=rate_type, current_only=True)
    for r in rows:
        try:
            r["value"] = json.loads(r["value"])
        except (TypeError, ValueError):
            pass
    return rows
