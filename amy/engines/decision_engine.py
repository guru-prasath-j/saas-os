"""Decision Engine (full) — PIOS intelligence.

A richer, layered Decision Engine built on top of the existing decision store.
It does NOT replace ``amy.intelligence.decisions.DecisionEngine`` — both write
the same `decisions` table, so they interoperate. This engine adds:

  * categories (career / finance / health / learning / projects / personal)
  * decision history (per category)
  * decision analysis  (resolution & confidence patterns)
  * decision recommendations (heuristic, derived from your own history)
  * outcome tracking

Responsibilities map (from spec):
  decision history          -> history()
  decision analysis         -> analyze()
  decision recommendations  -> recommend()
  outcome tracking          -> set_outcome() + analyze()['resolution_rate']
"""
from __future__ import annotations

from ..models.decision_model import Decision, CATEGORIES
from ..repositories.decision_repository import DecisionRepository

# words that, if present in an outcome note, hint the decision went well / badly
_POSITIVE = ("good", "great", "worked", "success", "happy", "right", "win",
             "positive", "glad", "paid off", "correct", "best")
_NEGATIVE = ("bad", "regret", "wrong", "mistake", "fail", "failed", "lost",
             "worse", "unhappy", "negative", "should not", "shouldn't")


def _outcome_polarity(outcome: str | None) -> int:
    """+1 good, -1 bad, 0 unknown — from the free-text outcome note."""
    if not outcome:
        return 0
    o = outcome.lower()
    pos = any(w in o for w in _POSITIVE)
    neg = any(w in o for w in _NEGATIVE)
    if pos and not neg:
        return 1
    if neg and not pos:
        return -1
    return 0


class DecisionEngine:
    def __init__(self, collab_db, events=None):
        self.repo = DecisionRepository(collab_db)
        self.events = events

    # --- writes ---------------------------------------------------------
    def record(self, title: str, category: str = "personal", reason: str = "",
               confidence: float | None = None) -> str:
        d = Decision.new(title=title, category=category, reason=reason, confidence=confidence)
        self.repo.add(d)
        if self.events is not None:
            try:
                self.events.emit("decision.recorded",
                                 {"id": d.id, "title": title, "category": d.category},
                                 source="decision")
            except Exception:
                pass
        return d.id

    def set_outcome(self, decision_id: str, outcome: str, status: str = "resolved") -> None:
        self.repo.set_outcome(decision_id, outcome, status)
        if self.events is not None:
            try:
                self.events.emit("decision.resolved",
                                 {"id": decision_id, "status": status}, source="decision")
            except Exception:
                pass

    # --- reads ----------------------------------------------------------
    def get(self, decision_id: str) -> dict | None:
        d = self.repo.get(decision_id)
        return d.to_dict() if d else None

    def history(self, category: str | None = None, limit: int = 200) -> list[dict]:
        return [d.to_dict() for d in self.repo.all(category=category, limit=limit)]

    # --- analysis -------------------------------------------------------
    def analyze(self) -> dict:
        """Aggregate patterns across all recorded decisions."""
        decisions = self.repo.all(limit=1000)
        total = len(decisions)
        by_category: dict[str, dict] = {}
        for cat in CATEGORIES:
            by_category[cat] = {"count": 0, "open": 0, "resolved": 0,
                                "good": 0, "bad": 0, "confidence_sum": 0.0,
                                "confidence_n": 0}

        resolved = good = bad = 0
        conf_sum = 0.0
        conf_n = 0
        for d in decisions:
            cat = d.category if d.category in by_category else "personal"
            b = by_category[cat]
            b["count"] += 1
            if d.status == "open":
                b["open"] += 1
            else:
                b["resolved"] += 1
                resolved += 1
            pol = _outcome_polarity(d.outcome)
            if pol > 0:
                b["good"] += 1; good += 1
            elif pol < 0:
                b["bad"] += 1; bad += 1
            if d.confidence is not None:
                b["confidence_sum"] += float(d.confidence); b["confidence_n"] += 1
                conf_sum += float(d.confidence); conf_n += 1

        # finalize per-category averages + success rate
        cat_summary = {}
        for cat, b in by_category.items():
            if b["count"] == 0:
                continue
            judged = b["good"] + b["bad"]
            cat_summary[cat] = {
                "count": b["count"],
                "open": b["open"],
                "resolved": b["resolved"],
                "success_rate": round(b["good"] / judged, 3) if judged else None,
                "avg_confidence": round(b["confidence_sum"] / b["confidence_n"], 3)
                if b["confidence_n"] else None,
            }

        judged_total = good + bad
        return {
            "total": total,
            "resolved": resolved,
            "open": total - resolved,
            "resolution_rate": round(resolved / total, 3) if total else None,
            "success_rate": round(good / judged_total, 3) if judged_total else None,
            "avg_confidence": round(conf_sum / conf_n, 3) if conf_n else None,
            "by_category": cat_summary,
        }

    # --- recommendations ------------------------------------------------
    def recommend(self, category: str | None = None) -> list[str]:
        """Heuristic, self-referential advice derived from your own history."""
        a = self.analyze()
        recs: list[str] = []
        if a["total"] == 0:
            return ["No decisions logged yet. Start recording decisions with a "
                    "reason and a confidence level so PIOS can learn your patterns."]

        # open backlog
        if a["open"] >= 5:
            recs.append(f"You have {a['open']} open decisions without a recorded "
                        f"outcome. Revisit and close them so analysis stays meaningful.")

        # overall calibration: high confidence but low success → overconfident
        sr, ac = a["success_rate"], a["avg_confidence"]
        if sr is not None and ac is not None:
            if ac >= 0.7 and sr < 0.5:
                recs.append("Your average confidence is high but outcomes are mixed — "
                            "you may be over-confident. Slow down on high-stakes calls.")
            elif ac <= 0.4 and sr >= 0.6:
                recs.append("Your outcomes are good despite low confidence — trust your "
                            "judgment a little more.")

        # per-category guidance
        cats = [category] if category in CATEGORIES else CATEGORIES
        for cat in cats:
            c = a["by_category"].get(cat)
            if not c:
                continue
            if c["success_rate"] is not None and c["success_rate"] < 0.4 and c["count"] >= 3:
                recs.append(f"'{cat}' decisions have a low success rate "
                            f"({int(c['success_rate']*100)}%). Consider a checklist or "
                            f"a second opinion before committing.")
            if c["open"] >= 3:
                recs.append(f"You have {c['open']} unresolved '{cat}' decisions — "
                            f"close the loop on these.")

        if not recs:
            recs.append("Your decision-making looks balanced. Keep logging outcomes "
                        "to sharpen future recommendations.")
        return recs
