"""Jurisdiction packs (Phase R7B) — data-driven country/rule configuration.

NO jurisdiction logic lives in Python: every rate, threshold, deadline,
calendar choice, financing-model list, and screening default comes from a
JSON pack in this directory. Adding jurisdiction #4 = dropping one new JSON
file here (see docs/jurisdictions.md for the template) — list_packs() picks
it up automatically.

Effective-date versioning: anything that changes over time (rates, dates,
thresholds) lives in a "versions" array; resolve_version() picks the entry
active on a given date, so packs can carry both last year's and this year's
rules. This extends the rate_table effective_from/effective_to pattern.
"""
from __future__ import annotations

import datetime as _dt
import json
from functools import lru_cache
from pathlib import Path

_PACK_DIR = Path(__file__).parent

REQUIRED_KEYS = ("id", "name", "currency", "fiscal_year", "calendar_systems",
                 "financing_models", "default_screening_profiles",
                 "obligation_presets", "compliance_deadlines", "disclaimer")

# Phase 4 (Loan Underwriting prep) — loan_config is OPTIONAL: a pack can
# exist without it (e.g. a future minimal jurisdiction with no loan
# support), so it's not in REQUIRED_KEYS above. When present it must be
# well-formed, same "optional-but-validated-if-there" treatment
# obligation_presets/compliance_deadlines already get in validate_pack().
LOAN_CONFIG_REQUIRED_KEYS = ("_disclaimer", "loan_limits", "minimum_income",
                            "max_debt_to_income_ratio", "late_fee_rules",
                            "interest_calculation_defaults",
                            "islamic_finance_available")
LOAN_TYPES = ("personal", "home", "business", "auto", "education")
INTEREST_CALCULATION_METHODS = ("simple", "compound", "reducing_balance")


class PackError(ValueError):
    """Malformed jurisdiction pack."""


# ---------------------------------------------------------------------------
# Loading + validation
# ---------------------------------------------------------------------------

def validate_pack(data: dict) -> list[str]:
    """Return a list of problems (empty = valid). Used by tests and by the
    docs template so a new pack can be checked before shipping."""
    problems = []
    for k in REQUIRED_KEYS:
        if k not in data:
            problems.append(f"missing required key: {k}")
    cur = data.get("currency") or {}
    for k in ("code", "symbol", "grouping"):
        if not cur.get(k):
            problems.append(f"currency.{k} is required")
    if cur.get("grouping") not in (None, "western", "indian"):
        problems.append("currency.grouping must be 'western' or 'indian'")
    fy = data.get("fiscal_year") or {}
    if fy.get("calendar") == "fiscal" and not 1 <= int(fy.get("start_month", 0)) <= 12:
        problems.append("fiscal_year.start_month must be 1-12")
    from ..calendars import list_systems
    for cs in data.get("calendar_systems", []):
        if cs not in list_systems():
            problems.append(f"unknown calendar system: {cs}")
    for section in ("obligation_presets", "compliance_deadlines"):
        for item in data.get(section, []) or []:
            if not item.get("id"):
                problems.append(f"{section} entry missing id")
            versions = item.get("versions") or []
            if not versions:
                problems.append(f"{section}.{item.get('id')}: no versions")
            for v in versions:
                if not v.get("effective_from"):
                    problems.append(
                        f"{section}.{item.get('id')}: version missing effective_from")
    if "loan_config" in data:
        problems.extend(validate_loan_config(data))
    return problems


def validate_loan_config(pack: dict) -> list[str]:
    """Return a list of problems (empty = valid) for pack['loan_config'].
    Called from validate_pack() whenever the key is present (loan_config
    itself is optional — see LOAN_CONFIG_REQUIRED_KEYS' comment above)."""
    cfg = pack.get("loan_config")
    if cfg is None:
        return ["pack has no loan_config section"]
    problems = []
    for k in LOAN_CONFIG_REQUIRED_KEYS:
        if k not in cfg:
            problems.append(f"loan_config missing required key: {k}")

    limits = cfg.get("loan_limits") or {}
    for lt in LOAN_TYPES:
        entry = limits.get(lt)
        if not entry:
            problems.append(f"loan_config.loan_limits missing entry for {lt!r}")
            continue
        if not isinstance(entry.get("amount"), (int, float)) or entry["amount"] <= 0:
            problems.append(f"loan_config.loan_limits.{lt}.amount must be a positive number")
        if not entry.get("currency"):
            problems.append(f"loan_config.loan_limits.{lt}.currency is required")

    mi = cfg.get("minimum_income") or {}
    if not isinstance(mi.get("amount"), (int, float)) or mi.get("amount", -1) < 0:
        problems.append("loan_config.minimum_income.amount must be a non-negative number")
    if not mi.get("currency"):
        problems.append("loan_config.minimum_income.currency is required")
    if mi.get("basis") not in ("annual", "monthly"):
        problems.append("loan_config.minimum_income.basis must be 'annual' or 'monthly'")

    dti = cfg.get("max_debt_to_income_ratio")
    if not isinstance(dti, (int, float)) or not 0 < dti <= 1:
        problems.append("loan_config.max_debt_to_income_ratio must be a number in (0, 1]")

    fee = cfg.get("late_fee_rules") or {}
    fee_type = fee.get("type")
    if fee_type not in ("percentage_of_overdue", "flat_fee"):
        problems.append("loan_config.late_fee_rules.type must be "
                        "'percentage_of_overdue' or 'flat_fee'")
    elif fee_type == "percentage_of_overdue" and not isinstance(fee.get("rate"), (int, float)):
        problems.append("loan_config.late_fee_rules.rate is required for percentage_of_overdue")
    elif fee_type == "flat_fee" and not isinstance(fee.get("amount"), (int, float)):
        problems.append("loan_config.late_fee_rules.amount is required for flat_fee")
    if not isinstance(fee.get("grace_days"), int):
        problems.append("loan_config.late_fee_rules.grace_days must be an integer")

    if cfg.get("interest_calculation_defaults") not in INTEREST_CALCULATION_METHODS:
        problems.append("loan_config.interest_calculation_defaults must be one of "
                        + "|".join(INTEREST_CALCULATION_METHODS))
    if not isinstance(cfg.get("islamic_finance_available"), bool):
        problems.append("loan_config.islamic_finance_available must be a boolean")
    return problems


def loan_config(pack: dict) -> dict | None:
    """The pack's Phase-4 loan-config extension, or None if this pack
    hasn't been extended with one. All figures inside are illustrative
    (see pack["loan_config"]["_disclaimer"]) — Phase 5 (Loan Underwriting)
    reads this; this module never computes anything with it."""
    return pack.get("loan_config")


@lru_cache(maxsize=None)
def load_pack(pack_id: str) -> dict:
    path = _PACK_DIR / f"{pack_id.lower()}.json"
    if not path.exists():
        raise PackError(f"no jurisdiction pack {pack_id!r} "
                        f"(expected {path.name} in amy/jurisdictions/)")
    data = json.loads(path.read_text(encoding="utf-8"))
    problems = validate_pack(data)
    if problems:
        raise PackError(f"pack {pack_id!r} invalid: " + "; ".join(problems))
    return data


def list_packs() -> list[dict]:
    out = []
    for p in sorted(_PACK_DIR.glob("*.json")):
        try:
            d = load_pack(p.stem)
            out.append({"id": d["id"], "name": d["name"],
                        "currency": d["currency"]["code"],
                        "calendar_systems": d["calendar_systems"]})
        except PackError:
            continue   # a broken pack must not hide the others
    return out


# ---------------------------------------------------------------------------
# Effective-date versioning
# ---------------------------------------------------------------------------

def resolve_version(versions: list[dict], on: _dt.date | None = None) -> dict | None:
    """Pick the version active on the given date (default today)."""
    on_s = (on or _dt.date.today()).isoformat()
    for v in versions or []:
        frm = v.get("effective_from") or "0000-01-01"
        to = v.get("effective_to") or "9999-12-31"
        if frm <= on_s <= to:
            return v
    return None


def obligation_preset(pack: dict, preset_id: str,
                      on: _dt.date | None = None) -> dict | None:
    """Base preset fields merged with the version active on the date."""
    for p in pack.get("obligation_presets", []):
        if p["id"] == preset_id:
            v = resolve_version(p.get("versions"), on)
            if v is None:
                return None
            merged = {k: val for k, val in p.items() if k != "versions"}
            merged.update(v)
            merged["jurisdiction"] = pack["id"]
            merged["disclaimer"] = pack["disclaimer"]
            return merged
    return None


def list_obligation_presets(pack: dict, on: _dt.date | None = None) -> list[dict]:
    out = []
    for p in pack.get("obligation_presets", []):
        merged = obligation_preset(pack, p["id"], on)
        if merged:
            out.append(merged)
    return out


# ---------------------------------------------------------------------------
# Deadline calendar (upcoming concrete dates across a horizon)
# ---------------------------------------------------------------------------

def upcoming_deadlines(pack: dict, after: _dt.date | None = None,
                       horizon_days: int = 90) -> list[dict]:
    """Resolve the pack's compliance deadlines + obligation schedules into
    concrete upcoming dates within the horizon."""
    from ..calendars import get_calendar
    after = after or _dt.date.today()
    limit = after + _dt.timedelta(days=horizon_days)
    found: list[dict] = []

    def _add(name: str, kind: str, date: _dt.date, extra: dict):
        if after < date <= limit:
            found.append({"jurisdiction": pack["id"], "name": name,
                          "kind": kind, "date": date.isoformat(),
                          "days_away": (date - after).days, **extra})

    for dl in pack.get("compliance_deadlines", []):
        v = resolve_version(dl.get("versions"), after)
        if not v:
            continue
        cal = get_calendar(dl.get("calendar_system", "gregorian"))
        rec = v.get("recurrence")
        if rec == "monthly" and v.get("day"):
            months_span = horizon_days // 28 + 2
            for k in range(months_span):
                m0 = after.month - 1 + k
                year, month = after.year + m0 // 12, m0 % 12 + 1
                try:
                    cand = _dt.date(year, month, v["day"])
                except ValueError:
                    cand = _dt.date(year, month, 28)
                _add(dl["name"], "compliance", cand,
                     {"applies_to": dl.get("applies_to"), "note": v.get("note")})
        elif rec == "quarterly" and v.get("months"):
            for m in v["months"]:
                for year in (after.year, after.year + 1):
                    try:
                        cand = _dt.date(year, m, v.get("day", 28))
                    except ValueError:
                        continue
                    _add(dl["name"], "compliance", cand,
                         {"applies_to": dl.get("applies_to"), "note": v.get("note")})
        elif v.get("month") and v.get("day"):
            cand = cal.next_occurrence(v["month"], v["day"], after)
            _add(dl["name"], "compliance", cand,
                 {"applies_to": dl.get("applies_to"), "note": v.get("note")})

    for p in pack.get("obligation_presets", []):
        v = resolve_version(p.get("versions"), after)
        if not v:
            continue
        cal = get_calendar(p.get("calendar_system", "gregorian"),
                           **(p.get("calendar_config") or {}))
        for item in v.get("schedule", []):
            if item.get("month") and item.get("day"):
                cand = cal.next_occurrence(item["month"], item["day"], after)
                _add(f"{p['name']} — {item.get('label', '')}".strip(" —"),
                     "obligation", cand, {"preset_id": p["id"]})

    found.sort(key=lambda x: x["date"])
    return found
