"""FX conversion (Phase R7B) — pluggable rate source, cached daily, mockable.

Default source is a static seed table (rates vs USD) shipped as data in
amy/jurisdictions/fx_seed.json — clearly approximate, good enough for
planning displays and fully offline. Swap in a live source by passing any
callable `source() -> {"CUR": rate_vs_usd, ...}` (or set one at runtime);
results are cached per-day in the user-data directory so a live source is
hit at most once a day.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

_SEED_PATH = Path(__file__).parent / "jurisdictions" / "fx_seed.json"


def _load_seed() -> dict:
    data = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    return data["rates_vs_usd"]


class FxConverter:
    """convert(amount, "AED", "INR") via USD cross rates.

    source: optional callable returning {"CUR": units_per_USD, ...}.
    cache_dir: where the daily cache file lives (per-install, not per-user —
    FX rates are not user data).
    """

    def __init__(self, source=None, cache_dir: str | Path | None = None):
        self._source = source
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._rates: dict | None = None
        self._rates_date: str | None = None

    # --- rates ----------------------------------------------------------------

    def _cache_path(self) -> Path | None:
        return (self._cache_dir / "fx_cache.json") if self._cache_dir else None

    def rates(self) -> dict:
        """{"CUR": units per USD}; refreshed at most once per day."""
        today = _dt.date.today().isoformat()
        if self._rates is not None and self._rates_date == today:
            return self._rates

        cp = self._cache_path()
        if cp and cp.exists():
            try:
                cached = json.loads(cp.read_text(encoding="utf-8"))
                if cached.get("date") == today and cached.get("rates"):
                    self._rates, self._rates_date = cached["rates"], today
                    return self._rates
            except Exception:
                pass   # unreadable cache → refetch below

        rates = None
        if self._source is not None:
            try:
                rates = dict(self._source())
            except Exception:
                rates = None   # live source down → seed fallback
        if not rates:
            rates = _load_seed()
        rates["USD"] = 1.0

        self._rates, self._rates_date = rates, today
        if cp:
            try:
                cp.parent.mkdir(parents=True, exist_ok=True)
                cp.write_text(json.dumps({"date": today, "rates": rates}),
                              encoding="utf-8")
            except Exception:
                pass   # cache write is best-effort
        return rates

    # --- conversion --------------------------------------------------------------

    def rate(self, frm: str, to: str) -> float:
        frm, to = (frm or "USD").upper(), (to or "USD").upper()
        if frm == to:
            return 1.0
        rates = self.rates()
        if frm not in rates or to not in rates:
            missing = frm if frm not in rates else to
            raise ValueError(f"no FX rate for {missing!r} — add it to "
                             "amy/jurisdictions/fx_seed.json or the live source")
        # cross via USD: amount/frm_rate = USD; USD * to_rate = target
        return rates[to] / rates[frm]

    def convert(self, amount: float, frm: str, to: str) -> float:
        return round(float(amount) * self.rate(frm, to), 2)
