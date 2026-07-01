"""PersonalityEngine — learns *how* the user thinks and writes.

Produces a personality profile from observable data, with no ML and no external
calls. Everything is explainable:

  * writing_style    — quantified from the user's own note prose
  * preferences      — from stored prefs + most-used domains
  * habits           — most frequent activity kinds
  * priorities       — top domains by goals + activity
  * decision_pattern — confidence / decisiveness from the decisions table

This lets other components (Digital Twin, drafting features) imitate the user.
"""
from __future__ import annotations

import json
import re
import statistics


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[.!?]+", text or "") if s.strip()]


class PersonalityEngine:
    def __init__(self, notes, collab_db):
        self.notes = notes or []
        self.db = collab_db.conn

    # --- writing style --------------------------------------------------
    def writing_style(self) -> dict:
        bodies = [n.body for n in self.notes if getattr(n, "body", None)]
        sample = "\n".join(bodies)[:200_000]
        sents = _sentences(sample)
        words = re.findall(r"[A-Za-z']+", sample)
        if not words:
            return {"sample_words": 0, "tone": "unknown"}
        sent_lengths = [len(re.findall(r"[A-Za-z']+", s)) for s in sents] or [0]
        avg_sent = round(statistics.mean(sent_lengths), 1)
        unique_ratio = round(len(set(w.lower() for w in words)) / len(words), 3)
        exclam = sample.count("!")
        questions = sample.count("?")
        bullets = sample.count("\n- ") + sample.count("\n* ")
        long_words = sum(1 for w in words if len(w) >= 8)
        formality = round(long_words / len(words), 3)
        if avg_sent <= 10:
            verbosity = "concise"
        elif avg_sent <= 20:
            verbosity = "balanced"
        else:
            verbosity = "elaborate"
        tone = "formal" if formality >= 0.18 else "casual"
        return {
            "sample_words": len(words),
            "avg_sentence_length": avg_sent,
            "vocabulary_richness": unique_ratio,
            "verbosity": verbosity,
            "tone": tone,
            "uses_bullets": bullets > 5,
            "exclamation_rate": round(exclam / max(1, len(sents)), 3),
            "question_rate": round(questions / max(1, len(sents)), 3),
        }

    # --- preferences ----------------------------------------------------
    def preferences(self) -> dict:
        prefs = {}
        try:
            for r in self.db.execute("SELECT key, value FROM prefs").fetchall():
                v = r["value"]
                try:
                    v = json.loads(v)
                except Exception:
                    pass
                prefs[r["key"]] = v
        except Exception:
            pass
        return prefs

    # --- habits ---------------------------------------------------------
    def habits(self) -> list[str]:
        rows = self.db.execute(
            "SELECT kind, COUNT(*) c FROM activities GROUP BY kind ORDER BY c DESC LIMIT 5"
        ).fetchall()
        return [r["kind"] for r in rows if r["kind"]]

    # --- priorities -----------------------------------------------------
    def priorities(self) -> list[str]:
        score: dict[str, float] = {}
        # goals weigh heavily
        for r in self.db.execute(
                "SELECT domain, COUNT(*) c FROM goals WHERE status!='done' GROUP BY domain").fetchall():
            if r["domain"]:
                score[r["domain"]] = score.get(r["domain"], 0) + 2.0 * r["c"]
        # recent activity weighs lightly
        for r in self.db.execute(
                "SELECT domain, COUNT(*) c FROM activities GROUP BY domain").fetchall():
            if r["domain"]:
                score[r["domain"]] = score.get(r["domain"], 0) + 0.2 * r["c"]
        return [d for d, _ in sorted(score.items(), key=lambda x: -x[1])[:5]]

    # --- decision pattern ----------------------------------------------
    def decision_pattern(self) -> dict:
        rows = self.db.execute(
            "SELECT confidence, status FROM decisions").fetchall()
        confs = [r["confidence"] for r in rows if r["confidence"] is not None]
        resolved = sum(1 for r in rows if r["status"] and r["status"] != "open")
        avg_conf = round(statistics.mean(confs), 3) if confs else None
        if avg_conf is None:
            style = "unknown"
        elif avg_conf >= 0.7:
            style = "decisive"
        elif avg_conf >= 0.4:
            style = "measured"
        else:
            style = "cautious"
        return {
            "decisions_logged": len(rows),
            "avg_confidence": avg_conf,
            "decisiveness": style,
            "follow_through_rate": round(resolved / len(rows), 3) if rows else None,
        }

    # --- full profile ---------------------------------------------------
    def profile(self) -> dict:
        return {
            "writing_style": self.writing_style(),
            "preferences": self.preferences(),
            "habits": self.habits(),
            "priorities": self.priorities(),
            "decision_pattern": self.decision_pattern(),
        }
