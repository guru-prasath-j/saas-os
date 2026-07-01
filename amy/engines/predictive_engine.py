"""Predictive Engine (PIOS) — heuristic forecasting.

Forecasts are *honest projections*, not ML. They extrapolate from data already
in the user's collab store (goals, milestones, tasks, activities) using simple,
explainable rules. Every forecast returns its `basis` so the reasoning is
transparent, and a `confidence` reflecting how much data backed it.

Supported forecasts:
  * goal completion   -> forecast_goal(goal_id) / forecast_goals()
  * learning progress -> forecast_learning()
  * career growth     -> forecast_career()
  * productivity      -> forecast_productivity()
"""
from __future__ import annotations

import datetime as _dt


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _parse(ts: str | None) -> _dt.datetime | None:
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _conf(n: int, span_days: float) -> float:
    """More data points over a longer span -> higher confidence (cap 0.9)."""
    base = min(0.6, 0.1 + 0.1 * n)
    if span_days >= 14:
        base += 0.2
    elif span_days >= 7:
        base += 0.1
    return round(min(0.9, base), 2)


class PredictiveEngine:
    def __init__(self, collab_db):
        self.db = collab_db.conn if collab_db is not None else None

    # --- goals ----------------------------------------------------------
    def forecast_goal(self, goal_id: str) -> dict | None:
        g = self.db.execute(
            "SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        if not g:
            return None
        return self._goal_eta(dict(g))

    def forecast_goals(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM goals WHERE status!='done' ORDER BY created_at").fetchall()
        out = []
        for r in rows:
            f = self._goal_eta(dict(r))
            if f:
                out.append(f)
        return out

    def _goal_eta(self, g: dict) -> dict:
        progress = float(g.get("progress") or 0.0)  # 0..1
        created = _parse(g.get("created_at"))
        now = _now()
        elapsed_days = (now - created).days + 1 if created else None

        # Prefer progress velocity; fall back to milestone completion rate.
        eta_days = None
        basis = ""
        if progress > 0 and elapsed_days and elapsed_days >= 1:
            rate = progress / elapsed_days  # progress per day
            remaining = max(0.0, 1.0 - progress)
            if rate > 0:
                eta_days = round(remaining / rate)
                basis = (f"{int(progress*100)}% done over {elapsed_days}d "
                         f"({round(rate*100,2)}%/day)")
        if eta_days is None:
            ms = self.db.execute(
                "SELECT done FROM milestones WHERE goal_id=?", (g["id"],)).fetchall()
            if ms:
                done = sum(1 for m in ms if m["done"])
                frac = done / len(ms)
                if done and elapsed_days:
                    rate = frac / elapsed_days
                    if rate > 0:
                        eta_days = round((1 - frac) / rate)
                        basis = f"{done}/{len(ms)} milestones in {elapsed_days}d"
        result = {
            "goal_id": g["id"], "title": g.get("title"),
            "progress": progress, "eta_days": eta_days,
            "projected_date": (now + _dt.timedelta(days=eta_days)).date().isoformat()
            if eta_days is not None else None,
            "basis": basis or "insufficient progress data",
            "confidence": _conf(1 if eta_days is not None else 0, elapsed_days or 0),
        }
        # On-track check vs target_date
        target = _parse(g.get("target_date"))
        if target and eta_days is not None:
            projected = now + _dt.timedelta(days=eta_days)
            result["on_track"] = projected <= target
        return result

    # --- learning -------------------------------------------------------
    def forecast_learning(self) -> dict:
        return self._activity_trend(
            domain="learning",
            label="learning",
            good="Your learning pace is steady or accelerating.",
            bad="Learning activity is slowing — schedule study blocks to recover.")

    # --- career ---------------------------------------------------------
    def forecast_career(self) -> dict:
        out = self._activity_trend(
            domain="career",
            label="career",
            good="Career activity is trending up — momentum is in your favour.",
            bad="Career activity has dipped — revisit applications / networking.")
        # blend in career goals' ETA if any
        goals = self.db.execute(
            "SELECT * FROM goals WHERE domain='career' AND status!='done'").fetchall()
        if goals:
            etas = [self._goal_eta(dict(g)).get("eta_days") for g in goals]
            etas = [e for e in etas if e is not None]
            if etas:
                out["nearest_career_goal_days"] = min(etas)
        return out

    # --- productivity ---------------------------------------------------
    def forecast_productivity(self) -> dict:
        return self._activity_trend(
            domain=None,
            label="productivity",
            good="Overall activity is healthy and trending up.",
            bad="Overall activity is declining week-over-week.")

    # --- finance cash-flow forecast ------------------------------------
    def forecast_finance(self, finance_engine) -> dict:
        """
        Project next-week spending from the last two 7-day windows and alert
        if the projection exceeds a comfortable weekly budget (monthly income / 4
        with a 10% safety buffer).

        Mirrors the _activity_trend() pattern but works with rupee amounts
        rather than activity counts.

        Returns:
          {
            "metric": "cash_flow",
            "this_week_spend": float,
            "prev_week_spend": float,
            "trend": "up" | "down" | "flat",
            "projected_next_week_spend": float,
            "monthly_income": float,
            "comfortable_weekly": float,
            "alert": bool,
            "note": str,
            "confidence": float,
          }
        """
        now = _now()
        wk1_start = (now - _dt.timedelta(days=7)).date().isoformat()
        wk2_start = (now - _dt.timedelta(days=14)).date().isoformat()
        today_str  = now.date().isoformat()

        txns_14d = finance_engine.list_transactions(limit=2000, since=wk2_start,
                                                    until=today_str)

        this_week_spend = sum(
            abs(t["amount"]) for t in txns_14d
            if t["amount"] < 0 and t["date"] >= wk1_start
        )
        prev_week_spend = sum(
            abs(t["amount"]) for t in txns_14d
            if t["amount"] < 0 and t["date"] < wk1_start
        )

        monthly_income = finance_engine.monthly_income()
        comfortable_weekly = (monthly_income / 4) * 1.1   # 10% buffer

        delta = this_week_spend - prev_week_spend
        projected = max(0.0, round(this_week_spend + delta, 2))

        if prev_week_spend == 0 and this_week_spend == 0:
            trend = "flat"
        elif this_week_spend >= prev_week_spend:
            trend = "up"
        else:
            trend = "down"

        alert = bool(comfortable_weekly > 0 and projected > comfortable_weekly)
        n_points = int(this_week_spend > 0) + int(prev_week_spend > 0)

        if alert:
            note = (
                f"Projected next-week spend (₹{projected:,.0f}) exceeds your "
                f"comfortable weekly budget (₹{comfortable_weekly:,.0f}). "
                f"Consider slowing discretionary spending."
            )
        elif trend == "down":
            note = "Spending is trending down — you're on track."
        elif trend == "flat" or n_points == 0:
            note = "Not enough spend data to forecast cash flow yet."
        else:
            note = (
                f"Spending is trending up this week (₹{this_week_spend:,.0f} vs "
                f"₹{prev_week_spend:,.0f} last week) but within comfortable range."
            )

        return {
            "metric": "cash_flow",
            "this_week_spend": round(this_week_spend, 2),
            "prev_week_spend": round(prev_week_spend, 2),
            "trend": trend,
            "projected_next_week_spend": projected,
            "monthly_income": round(monthly_income, 2),
            "comfortable_weekly": round(comfortable_weekly, 2),
            "alert": alert,
            "note": note,
            "confidence": _conf(n_points, 14 if n_points >= 2 else 7),
        }

    # --- shared trend engine -------------------------------------------
    def _activity_trend(self, domain, label, good, bad) -> dict:
        now = _now()
        wk1_start = now - _dt.timedelta(days=7)
        wk2_start = now - _dt.timedelta(days=14)
        if domain:
            rows = self.db.execute(
                "SELECT ts FROM activities WHERE domain=? AND ts>=?",
                (domain, wk2_start.isoformat())).fetchall()
        else:
            rows = self.db.execute(
                "SELECT ts FROM activities WHERE ts>=?",
                (wk2_start.isoformat(),)).fetchall()
        this_week = prev_week = 0
        for r in rows:
            t = _parse(r["ts"])
            if not t:
                continue
            if t >= wk1_start:
                this_week += 1
            elif t >= wk2_start:
                prev_week += 1
        if prev_week == 0 and this_week == 0:
            trend = "flat"; note = f"No recent {label} activity recorded."
        elif this_week >= prev_week:
            trend = "up"; note = good
        else:
            trend = "down"; note = bad
        # naive next-week projection = linear continuation
        delta = this_week - prev_week
        projection = max(0, this_week + delta)
        return {
            "metric": label, "this_week": this_week, "prev_week": prev_week,
            "trend": trend, "projected_next_week": projection, "note": note,
            "confidence": _conf(this_week + prev_week, 14),
        }
