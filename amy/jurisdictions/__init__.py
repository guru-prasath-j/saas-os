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
    return problems


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
