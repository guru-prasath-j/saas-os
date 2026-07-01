"""Transaction deduplication engine.

Finds duplicate transactions that were imported multiple times (re-upload,
CSV + Gmail same txn, value-date vs posting-date off-by-one, etc.).

Confidence levels:
  exact  — identical date + amount + merchant + account  (safe auto-delete)
  near   — same date + amount + account, description tokens overlap ≥ 40%
  fuzzy  — date within ±1 day + amount + account + tokens overlap ≥ 50%
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date as _date

# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

_NOISE = frozenset({
    "neft", "imps", "rtgs", "upi", "transfer", "fund", "payment", "debit", "credit",
    "dr", "cr", "your", "account", "acc", "bank", "towards", "for", "from", "to",
    "the", "via", "rs", "inr", "amount", "txn", "ref", "no", "by", "on", "at", "in",
    "has", "been", "processed", "successfully", "using",
})


def _sig(text: str) -> frozenset:
    """Meaningful tokens: non-noise words (≥3 chars) + long reference numbers."""
    words = frozenset(
        w.upper() for w in re.findall(r"[A-Za-z]{3,}", text)
        if w.lower() not in _NOISE
    )
    nums = frozenset(re.findall(r"\d{8,}", text))
    return words | nums


def _overlap(s1: frozenset, s2: frozenset) -> float:
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    return len(s1 & s2) / len(s1 | s2)


# ---------------------------------------------------------------------------
# Core scan
# ---------------------------------------------------------------------------

def find_duplicates(conn, date_window: int = 1) -> list[dict]:
    """
    Scan all transactions in conn and return duplicate groups.

    Each group dict:
      confidence   : "exact" | "near" | "fuzzy"
      reason       : human-readable explanation
      count        : total transactions in group
      keep_id      : rowid of the transaction to keep (oldest insert)
      transactions : list of row dicts (id, date, amount, merchant,
                     account_id, source, category, rowid)
    """
    rows = conn.execute(
        "SELECT rowid, id, date, amount, merchant, account_id, source, category "
        "FROM transactions ORDER BY rowid"
    ).fetchall()

    cols = ("rowid", "id", "date", "amount", "merchant",
            "account_id", "source", "category")
    txns = [dict(zip(cols, r)) for r in rows]

    # Bucket by (account_id, amount) — must match on both to be a candidate
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for t in txns:
        key = (t["account_id"] or "", round(float(t["amount"]), 2))
        buckets[key].append(t)

    groups: list[dict] = []
    seen: set = set()

    for bucket in buckets.values():
        if len(bucket) < 2:
            continue

        for i, t1 in enumerate(bucket):
            if t1["id"] in seen:
                continue

            try:
                d1 = _date.fromisoformat(t1["date"])
            except ValueError:
                continue

            sig1 = _sig(t1["merchant"] or "")
            grp = [t1]

            for t2 in bucket[i + 1:]:
                if t2["id"] in seen:
                    continue
                try:
                    d2 = _date.fromisoformat(t2["date"])
                except ValueError:
                    continue

                day_diff = abs((d1 - d2).days)
                if day_diff > date_window:
                    continue

                sig2 = _sig(t2["merchant"] or "")
                ov = _overlap(sig1, sig2)

                if t1["merchant"] == t2["merchant"] and day_diff == 0:
                    grp.append(t2)
                elif ov >= 0.4 and day_diff == 0:
                    grp.append(t2)
                elif ov >= 0.5 and day_diff <= date_window:
                    grp.append(t2)

            if len(grp) < 2:
                continue

            for t in grp:
                seen.add(t["id"])

            # Sort oldest-first by rowid so default "keep" = first entry
            grp.sort(key=lambda t: t["rowid"])

            # Classify group confidence
            all_exact = all(
                t["merchant"] == grp[0]["merchant"] and t["date"] == grp[0]["date"]
                for t in grp[1:]
            )
            dates = {t["date"] for t in grp}
            if all_exact:
                confidence = "exact"
                reason = "Identical date, amount, and description"
            elif len(dates) > 1:
                confidence = "fuzzy"
                reason = (
                    f"Same amount, similar descriptions, "
                    f"dates differ by {max((abs((_date.fromisoformat(t['date']) - _date.fromisoformat(grp[0]['date'])).days) for t in grp[1:]))}"
                    " day(s) — possible value-date vs posting-date"
                )
            else:
                confidence = "near"
                reason = "Same date and amount, similar descriptions — possible re-import"

            groups.append({
                "confidence": confidence,
                "reason": reason,
                "count": len(grp),
                "keep_id": grp[0]["id"],
                "transactions": grp,
            })

    # Sort: exact first (highest confidence), then near, then fuzzy
    _order = {"exact": 0, "near": 1, "fuzzy": 2}
    groups.sort(key=lambda g: _order[g["confidence"]])
    return groups


def auto_resolve_exact(conn) -> int:
    """Delete all exact duplicates, keeping the oldest insert (lowest rowid). Returns deleted count."""
    groups = find_duplicates(conn, date_window=0)
    exact = [g for g in groups if g["confidence"] == "exact"]

    delete_ids: list[str] = []
    for g in exact:
        # grp is already sorted oldest-first; delete everything after index 0
        delete_ids.extend(t["id"] for t in g["transactions"][1:])

    if not delete_ids:
        return 0

    placeholders = ",".join("?" * len(delete_ids))
    count = conn.execute(
        f"DELETE FROM transactions WHERE id IN ({placeholders})", delete_ids
    ).rowcount
    conn.commit()
    return count
