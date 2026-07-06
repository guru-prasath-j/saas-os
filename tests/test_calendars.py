"""Phase R7A-3 — calendar abstraction: period math for all three systems."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.calendars import get_calendar, list_systems


def test_registry():
    assert list_systems() == ["fiscal", "gregorian", "hijri"]
    with pytest.raises(ValueError, match="unknown calendar"):
        get_calendar("mayan")


def test_gregorian_year_period():
    p = get_calendar("gregorian").year_period(dt.date(2026, 7, 6))
    assert p.start == dt.date(2026, 1, 1)
    assert p.end == dt.date(2026, 12, 31)
    assert p.label == "2026" and p.days == 365
    assert p.contains(dt.date(2026, 7, 6))
    assert 0.50 < p.elapsed_fraction(dt.date(2026, 7, 6)) < 0.53


def test_fiscal_april_year_period():
    cal = get_calendar("fiscal", start_month=4)          # India Apr–Mar
    p = cal.year_period(dt.date(2026, 7, 6))
    assert p.start == dt.date(2026, 4, 1)
    assert p.end == dt.date(2027, 3, 31)
    assert p.label == "FY2026-27"
    # before April → previous fiscal year
    p2 = cal.year_period(dt.date(2026, 2, 10))
    assert p2.start == dt.date(2025, 4, 1) and p2.end == dt.date(2026, 3, 31)


def test_fiscal_january_equals_calendar_year():
    p = get_calendar("fiscal", start_month=1).year_period(dt.date(2026, 7, 6))
    assert p.start == dt.date(2026, 1, 1) and p.end == dt.date(2026, 12, 31)


def test_hijri_year_period_is_lunar():
    cal = get_calendar("hijri")
    d = dt.date(2026, 7, 6)
    p = cal.year_period(d)
    assert p.contains(d)
    assert 353 <= p.days <= 356            # lunar year ≈ 354-355 days
    assert p.label.endswith("AH")
    # consecutive periods must tile: day after end starts the next year
    nxt = cal.year_period(p.end + dt.timedelta(days=1))
    assert nxt.start == p.end + dt.timedelta(days=1)


def test_add_years():
    g = get_calendar("gregorian")
    assert g.add_years(dt.date(2024, 2, 29), 1) == dt.date(2025, 2, 28)
    h = get_calendar("hijri")
    d = dt.date(2026, 7, 6)
    later = h.add_years(d, 1)
    # one lunar year later lands ~354-355 days out (< a solar year)
    assert 350 <= (later - d).days <= 356


def test_next_occurrence():
    g = get_calendar("gregorian")
    assert g.next_occurrence(6, 15, dt.date(2026, 7, 6)) == dt.date(2027, 6, 15)
    assert g.next_occurrence(9, 15, dt.date(2026, 7, 6)) == dt.date(2026, 9, 15)
    # hijri named date (e.g. month 9 = Ramadan start) resolves to gregorian
    h = get_calendar("hijri")
    nxt = h.next_occurrence(9, 1, dt.date(2026, 7, 6))
    assert nxt > dt.date(2026, 7, 6)
    assert h.to_display(nxt).split("-")[1] == "09"
