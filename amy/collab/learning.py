"""Learning Agent — detect topic/domain trends over time and recommend.

Compares activity in a recent window vs the previous window of equal length and
labels each topic increasing / stable / decreasing, then makes recommendations.
"""
from __future__ import annotations

import datetime as _dt
from collections import Counter


class LearningAgent:
    def __init__(self, db, memory):
        self.db = db.conn
        self.memory = memory

    def record_topic(self, topic: str):
        self.memory.log_activity("topic", topic, domain=topic)

    def _counts(self, start: str, end: str) -> Counter:
        rs = self.db.execute(
            "SELECT domain FROM activities WHERE domain IS NOT NULL AND ts>=? AND ts<?",
            (start, end)).fetchall()
        return Counter(r["domain"] for r in rs)

    def trends(self, window_days: int = 7) -> dict:
        now = _dt.datetime.now(_dt.timezone.utc)
        mid = now - _dt.timedelta(days=window_days)
        start = now - _dt.timedelta(days=2 * window_days)
        recent = self._counts(mid.isoformat(), (now + _dt.timedelta(seconds=1)).isoformat())
        prior = self._counts(start.isoformat(), mid.isoformat())

        out = {}
        for topic in set(recent) | set(prior):
            r, p = recent.get(topic, 0), prior.get(topic, 0)
            if p == 0 and r > 0:
                label = "increasing"
            elif r == 0 and p > 0:
                label = "decreasing"
            else:
                ratio = (r + 1) / (p + 1)
                label = "increasing" if ratio >= 1.25 else "decreasing" if ratio <= 0.8 else "stable"
            out[topic] = {"recent": r, "prior": p, "trend": label}
        return out

    def recommendations(self, window_days: int = 7) -> list[str]:
        recs = []
        for topic, t in sorted(self.trends(window_days).items(),
                               key=lambda kv: -kv[1]["recent"]):
            if t["trend"] == "increasing":
                recs.append(f"'{topic}' is trending up — consider setting a goal or deep-diving.")
            elif t["trend"] == "decreasing":
                recs.append(f"'{topic}' is cooling off — revisit if it still matters.")
        return recs or ["Not enough activity yet to spot trends."]
