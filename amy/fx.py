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


def multi_currency_summary(fe, base: str, home_jurisdiction: str,
                           fx: "FxConverter") -> dict:
    """Per-currency (native) and per-jurisdiction (base-converted) totals.
    Custodial accounts are excluded — their money is never the user's own.
    Shared by GET /api/finance/overview/fx and the morning briefing."""
    import datetime as _dt2
    accounts = {a["id"]: a for a in fe.list_accounts()}
    rows = fe.conn.execute(
        "SELECT account_id, currency, amount, date FROM transactions").fetchall()
    month_start = _dt2.date.today().replace(day=1).isoformat()

    def _bucket_add(bucket: dict, key: str, amt: float, in_month: bool):
        b = bucket.setdefault(key, {"balance": 0.0, "month_in": 0.0,
                                    "month_out": 0.0})
        b["balance"] += amt
        if in_month:
            b["month_in" if amt > 0 else "month_out"] += abs(amt)

    by_currency: dict[str, dict] = {}
    by_jurisdiction: dict[str, dict] = {}
    unconvertible: set[str] = set()
    for r in rows:
        acc = accounts.get(r["account_id"]) or {}
        if acc.get("account_type") == "custodial":
            continue
        cur = (r["currency"] or acc.get("currency") or base).upper()
        jur = (acc.get("jurisdiction") or home_jurisdiction).lower()
        amt = float(r["amount"] or 0)
        in_month = (r["date"] or "") >= month_start
        _bucket_add(by_currency, cur, amt, in_month)
        try:
            _bucket_add(by_jurisdiction, jur, fx.convert(amt, cur, base), in_month)
        except ValueError:
            unconvertible.add(cur)

    currencies = {}
    total_base = 0.0
    for cur, b in by_currency.items():
        out = {k: round(v, 2) for k, v in b.items()}
        try:
            rate = fx.rate(cur, base)
            out["in_base"] = {k: round(v * rate, 2) for k, v in b.items()}
            total_base += out["in_base"]["balance"]
        except ValueError:
            out["in_base"] = None
        currencies[cur] = out

    return {
        "base_currency": base,
        "balance_estimate_base": round(total_base, 2),
        "by_currency": currencies,
        "by_jurisdiction_in_base": {
            jid: {k: round(v, 2) for k, v in b.items()}
            for jid, b in by_jurisdiction.items()},
        "unconvertible_currencies": sorted(unconvertible),
    }
