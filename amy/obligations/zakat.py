"""Zakat, end to end — the deep calculation behind the generic wealth_rate
preset (amy/obligations/__init__.py keeps the generic status; this module
does the full religiously-correct working):

  nisab   — live gold/silver spot price (85g gold / 595g silver), converted
            to the pack currency via FxConverter, cached daily; falls back
            to the pack's reference threshold when offline
  wealth  — per-account balances + investment holdings, custodial accounts
            excluded (hard rail — money held in trust is not the owner's)
  hawl    — one full LUNAR year above nisab, derived from the user's actual
            balance history and the Hijri calendar adapter, not assumed
  rate    — 2.5% of qualifying wealth once nisab + hawl are both satisfied

Every figure in the report carries its working (breakdown, prices, dates,
rules version) so the user — or an auditor — can verify by hand. Estimates,
never rulings: the pack disclaimer rides on every response.
"""
from __future__ import annotations

import datetime as _dt
import json
import urllib.request
from pathlib import Path

from ..calendars import get_calendar
from ..jurisdictions import load_pack, obligation_preset

NISAB_GOLD_GRAMS = 85.0
NISAB_SILVER_GRAMS = 595.0
_TROY_OUNCE_GRAMS = 31.1035
_PRICE_URL = "https://api.gold-api.com/price/{symbol}"   # free, no key
_ZAKAT_RATE_FALLBACK = 0.025


# ---------------------------------------------------------------------------
# Live nisab (daily-cached; offline falls back to the pack reference number)
# ---------------------------------------------------------------------------

def _fetch_spot_usd(symbol: str) -> float:
    """Spot price in USD/troy-oz for XAU (gold) or XAG (silver)."""
    req = urllib.request.Request(_PRICE_URL.format(symbol=symbol),
                                 headers={"User-Agent": "amy-personalos"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    price = float(data["price"])
    if price <= 0:
        raise ValueError(f"non-positive {symbol} price")
    return price


def nisab_value(currency: str = "INR", cache_dir: str | Path | None = None) -> dict:
    """Both nisab thresholds (gold + silver standards) in `currency`.
    Silver gives the lower threshold — the more cautious standard — so it's
    marked as the default the report compares against."""
    today = _dt.date.today().isoformat()
    cache_path = Path(cache_dir) / "nisab_cache.json" if cache_dir else None

    cached = None
    if cache_path and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cached = None
    if cached and cached.get("date") == today and cached.get("currency") == currency:
        return cached["nisab"]

    try:
        gold_oz = _fetch_spot_usd("XAU")
        silver_oz = _fetch_spot_usd("XAG")
        from ..fx import FxConverter
        fx = FxConverter(cache_dir=cache_dir)
        gold_gram = fx.convert(gold_oz / _TROY_OUNCE_GRAMS, "USD", currency)
        silver_gram = fx.convert(silver_oz / _TROY_OUNCE_GRAMS, "USD", currency)
        nisab = {
            "currency": currency,
            "gold": {"grams": NISAB_GOLD_GRAMS,
                     "price_per_gram": round(gold_gram, 2),
                     "threshold": round(NISAB_GOLD_GRAMS * gold_gram, 2)},
            "silver": {"grams": NISAB_SILVER_GRAMS,
                       "price_per_gram": round(silver_gram, 2),
                       "threshold": round(NISAB_SILVER_GRAMS * silver_gram, 2)},
            "default_standard": "silver",
            "source": "gold-api.com spot, FX via FxConverter",
            "fetched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "live": True,
        }
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(
                {"date": today, "currency": currency, "nisab": nisab}),
                encoding="utf-8")
        return nisab
    except Exception as exc:
        # yesterday's cache beats a static fallback
        if cached and cached.get("currency") == currency:
            stale = dict(cached["nisab"])
            stale["live"] = False
            stale["note"] = f"live fetch failed ({type(exc).__name__}) — using {cached.get('date')} prices"
            return stale
        return {"currency": currency, "gold": None, "silver": None,
                "default_standard": "pack_reference", "live": False,
                "note": f"live fetch failed ({type(exc).__name__}) — "
                        "using the jurisdiction pack's reference threshold"}


# ---------------------------------------------------------------------------
# Wealth breakdown (custodial hard-excluded; investment holdings included)
# ---------------------------------------------------------------------------

def wealth_breakdown(fe, eligible_types: list[str] | None = None) -> dict:
    eligible = set(eligible_types or ["savings", "current", "investment"])
    eligible.discard("custodial")            # hard rail, pack cannot override
    accounts, excluded, total = [], [], 0.0
    for a in fe.list_accounts():
        bal_row = fe.conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM transactions WHERE account_id=?",
            (a["id"],)).fetchone()
        bal = round(float(bal_row["s"] or 0), 2)
        atype = a.get("account_type") or "savings"
        entry = {"account": a.get("nickname"), "type": atype, "balance": bal}
        if atype == "custodial":
            entry["excluded_because"] = "held in trust — not the owner's wealth"
            excluded.append(entry)
        elif atype not in eligible:
            entry["excluded_because"] = f"account type '{atype}' not zakatable in this pack"
            excluded.append(entry)
        else:
            accounts.append(entry)
            total += bal
    # investment HOLDINGS (the investments table) are zakatable wealth too —
    # the generic engine only sums account transactions and misses these
    inv_row = fe.conn.execute(
        "SELECT COALESCE(SUM(current_value),0) s, COUNT(*) n FROM investments").fetchone()
    investments_value = round(float(inv_row["s"] or 0), 2)
    if investments_value:
        total += investments_value
    return {"accounts": accounts, "excluded": excluded,
            "investment_holdings": {"count": inv_row["n"],
                                    "value": investments_value},
            "total": round(total, 2)}


# ---------------------------------------------------------------------------
# Hawl — one full lunar year above nisab, from actual balance history
# ---------------------------------------------------------------------------

def hawl_status(fe, nisab_threshold: float,
                eligible_types: list[str] | None = None,
                on: _dt.date | None = None) -> dict:
    """Walk the transaction history (eligible accounts only) as a running
    balance and find the LAST date wealth crossed from below nisab to above
    and stayed there. Hawl completes one Hijri year after that crossing.
    Investment holdings have no dated history — they're treated as held
    since before the window (conservative: shortens, never extends, hawl)."""
    on = on or _dt.date.today()
    eligible = set(eligible_types or ["savings", "current", "investment"])
    eligible.discard("custodial")
    acc_ids = [a["id"] for a in fe.list_accounts()
               if (a.get("account_type") or "savings") in eligible
               and a.get("account_type") != "custodial"]
    inv_row = fe.conn.execute(
        "SELECT COALESCE(SUM(current_value),0) s FROM investments").fetchone()
    baseline = float(inv_row["s"] or 0)

    if not acc_ids:
        return {"state": "no_eligible_accounts"}
    marks = ",".join("?" * len(acc_ids))
    rows = fe.conn.execute(
        f"SELECT date, SUM(amount) delta FROM transactions"
        f" WHERE account_id IN ({marks}) GROUP BY date ORDER BY date",
        acc_ids).fetchall()
    if not rows:
        return {"state": "no_history"}

    running = baseline
    crossing = None
    for r in rows:
        prev = running
        running += float(r["delta"] or 0)
        if prev < nisab_threshold <= running:
            crossing = r["date"]
        elif running < nisab_threshold:
            crossing = None          # dipped below — hawl restarts
    if crossing is None:
        return {"state": "below_nisab",
                "note": "wealth is currently below nisab (or dipped below it, "
                        "which restarts the hawl)"}

    hijri = get_calendar("hijri")
    start = _dt.date.fromisoformat(str(crossing)[:10])
    completes = hijri.add_years(start, 1)
    days_left = (completes - on).days
    return {
        "state": "complete" if days_left <= 0 else "in_progress",
        "crossed_nisab_on": start.isoformat(),
        "crossed_nisab_on_hijri": hijri.to_display(start),
        "hawl_completes_on": completes.isoformat(),
        "hawl_completes_on_hijri": hijri.to_display(completes),
        "days_remaining": max(0, days_left),
        "note": "one full lunar (Hijri) year above nisab — ~354 days, "
                "computed with the Umm al-Qura calendar",
    }


# ---------------------------------------------------------------------------
# The full report
# ---------------------------------------------------------------------------

def zakat_report(fe, jurisdiction: str = "india",
                 cache_dir: str | Path | None = None,
                 on: _dt.date | None = None) -> dict:
    on = on or _dt.date.today()
    pack = load_pack(jurisdiction)
    preset = obligation_preset(pack, "zakat", on)
    currency = pack["currency"]["code"]
    rate = float((preset or {}).get("rate") or _ZAKAT_RATE_FALLBACK)
    eligible = (preset or {}).get("eligible_account_types")

    nisab = nisab_value(currency, cache_dir=cache_dir)
    if nisab.get("silver"):
        threshold = nisab["silver"]["threshold"]
        standard = "silver (595g) — the lower, more cautious standard"
    elif nisab.get("gold"):
        threshold = nisab["gold"]["threshold"]
        standard = "gold (85g)"
    else:
        threshold = float(((preset or {}).get("wealth_threshold") or {})
                          .get("amount") or 0)
        standard = "pack reference value (live price unavailable)"

    wealth = wealth_breakdown(fe, eligible)
    above = wealth["total"] >= threshold > 0
    hawl = hawl_status(fe, threshold, eligible, on) if above else {
        "state": "not_applicable", "note": "wealth below nisab — no hawl runs"}
    due = above and hawl.get("state") == "complete"
    liability = round(rate * wealth["total"], 2) if due else 0.0

    if not above:
        verdict = (f"No zakat is owed: qualifying wealth "
                   f"({currency} {wealth['total']:,.2f}) is below nisab "
                   f"({currency} {threshold:,.2f}, {standard}).")
    elif not due:
        verdict = (f"Wealth is above nisab but the hawl is not complete — "
                   f"{hawl.get('days_remaining', '?')} days until "
                   f"{hawl.get('hawl_completes_on')} "
                   f"({hawl.get('hawl_completes_on_hijri')}). "
                   f"If wealth stays above nisab until then, zakat of "
                   f"~{currency} {rate * wealth['total']:,.2f} will be due.")
    else:
        verdict = (f"Zakat is due: {currency} {liability:,.2f} "
                   f"(2.5% of {currency} {wealth['total']:,.2f}).")

    return {
        "computed_on": on.isoformat(),
        "computed_on_hijri": get_calendar("hijri").to_display(on),
        "jurisdiction": jurisdiction,
        "currency": currency,
        "nisab": nisab,
        "threshold_used": threshold,
        "threshold_standard": standard,
        "wealth": wealth,
        "above_nisab": above,
        "hawl": hawl,
        "rate": rate,
        "zakat_due_now": due,
        "estimated_liability": liability,
        "verdict": verdict,
        "rules_shown": (preset or {}).get("wealth_threshold") and {
            "rate": rate,
            "holding_period": "1 hijri year",
            "eligible_account_types": eligible,
            "effective_from": (preset or {}).get("effective_from"),
        },
        "disclaimer": (preset or {}).get(
            "disclaimer", pack.get("disclaimer",
                                   "Estimates only — verify with a scholar.")),
    }
