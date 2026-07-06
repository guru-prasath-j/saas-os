"""Calendar abstraction (Phase R7A-3).

Answers exactly two questions for any supported calendar system:
  1. What period is date X in (year / quarter), and when does it end?
  2. Date arithmetic in that system (add_years, next occurrence of a
     month/day) — what the obligations engine needs for holding periods
     ("one full lunar year") and pack-defined named dates.

Systems ship as adapter classes registered by name:
    gregorian            calendar year, Jan 1 – Dec 31
    hijri                Islamic lunar year (via the hijridate library)
    fiscal               configurable year start month (e.g. 4 = Apr–Mar)

Adding a NEW calendar system = one adapter class + a registry entry here.
Jurisdictions that use existing systems need JSON config only ("fiscal"
with start_month). No holidays are hardcoded — jurisdiction packs carry
named dates as data and resolve them through next_occurrence().
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

Date = _dt.date


@dataclass(frozen=True)
class Period:
    start: Date            # inclusive
    end: Date              # inclusive
    label: str
    system: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, d: Date) -> bool:
        return self.start <= d <= self.end

    def days_remaining(self, d: Date) -> int:
        return max(0, (self.end - d).days)

    def elapsed_fraction(self, d: Date) -> float:
        if not self.contains(d):
            return 1.0 if d > self.end else 0.0
        return ((d - self.start).days + 1) / self.days


class GregorianCalendar:
    name = "gregorian"

    def year_period(self, d: Date) -> Period:
        return Period(_dt.date(d.year, 1, 1), _dt.date(d.year, 12, 31),
                      str(d.year), self.name)

    def add_years(self, d: Date, n: int) -> Date:
        try:
            return d.replace(year=d.year + n)
        except ValueError:            # Feb 29 → Feb 28
            return d.replace(year=d.year + n, day=28)

    def next_occurrence(self, month: int, day: int, after: Date) -> Date:
        for year in (after.year, after.year + 1):
            try:
                cand = _dt.date(year, month, day)
            except ValueError:
                cand = _dt.date(year, month, 28)
            if cand > after:
                return cand
        raise ValueError("unreachable")

    def to_display(self, d: Date) -> str:
        return d.isoformat()


class FiscalCalendar(GregorianCalendar):
    """Gregorian dates, but the *year period* starts at a configured month
    (e.g. start_month=4 → April–March, the Indian fiscal year)."""
    name = "fiscal"

    def __init__(self, start_month: int = 4):
        if not 1 <= int(start_month) <= 12:
            raise ValueError("start_month must be 1-12")
        self.start_month = int(start_month)

    def year_period(self, d: Date) -> Period:
        if self.start_month == 1:
            p = GregorianCalendar().year_period(d)
            return Period(p.start, p.end, f"FY{d.year}", self.name)
        start_year = d.year if d.month >= self.start_month else d.year - 1
        start = _dt.date(start_year, self.start_month, 1)
        end = self.add_years(start, 1) - _dt.timedelta(days=1)
        label = f"FY{start_year}-{str(start_year + 1)[2:]}"
        return Period(start, end, label, self.name)


class HijriCalendar:
    """Islamic lunar calendar via the hijridate library (Umm al-Qura)."""
    name = "hijri"

    def _to_hijri(self, d: Date):
        from hijridate import Gregorian
        return Gregorian(d.year, d.month, d.day).to_hijri()

    def _to_greg(self, y: int, m: int, day: int) -> Date:
        from hijridate import Hijri
        g = Hijri(y, m, day).to_gregorian()
        return _dt.date(g.year, g.month, g.day)

    def year_period(self, d: Date) -> Period:
        h = self._to_hijri(d)
        start = self._to_greg(h.year, 1, 1)
        end = self._to_greg(h.year + 1, 1, 1) - _dt.timedelta(days=1)
        return Period(start, end, f"{h.year} AH", self.name)

    def add_years(self, d: Date, n: int) -> Date:
        h = self._to_hijri(d)
        day = min(h.day, 29)          # every hijri month has ≥29 days
        return self._to_greg(h.year + n, h.month, day)

    def next_occurrence(self, month: int, day: int, after: Date) -> Date:
        h = self._to_hijri(after)
        for year in (h.year, h.year + 1):
            try:
                cand = self._to_greg(year, month, min(day, 29))
            except Exception:
                continue
            if cand > after:
                return cand
        raise ValueError(f"no next occurrence for hijri {month}-{day}")

    def to_display(self, d: Date) -> str:
        h = self._to_hijri(d)
        return f"{h.year}-{h.month:02d}-{h.day:02d} AH"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_CALENDARS = {
    "gregorian": GregorianCalendar,
    "hijri": HijriCalendar,
    "fiscal": FiscalCalendar,
}


def get_calendar(name: str, **cfg):
    """get_calendar("fiscal", start_month=4) — cfg comes straight from
    jurisdiction-pack JSON, so packs configure calendars without code."""
    cls = _CALENDARS.get((name or "gregorian").lower())
    if cls is None:
        raise ValueError(f"unknown calendar system {name!r} — "
                         f"known: {', '.join(sorted(_CALENDARS))}")
    return cls(**cfg) if cfg else cls()


def list_systems() -> list[str]:
    return sorted(_CALENDARS)
