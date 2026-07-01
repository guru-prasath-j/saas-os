"""Unified Timeline Engine.

Merges chronological events from notes, activities, events, decisions, and
(optionally) the connectors — email / calendar / tasks. Supports filtering by
source, keyword search, grouping by day/week/month, and summaries.

Backward-compatible: build(notes, limit) keeps its original behavior.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict


def _short(payload: str, n: int = 80) -> str:
    try:
        d = json.loads(payload or "{}")
        if isinstance(d, dict):
            for key in ("title", "query", "detail", "name"):
                if d.get(key):
                    return str(d[key])[:n]
            return ", ".join(f"{k}={v}" for k, v in list(d.items())[:2])[:n]
    except Exception:
        pass
    return (payload or "")[:n]


class TimelineEngine:
    def __init__(self, collab_db):
        self.db = collab_db.conn

    # --- collect from all sources ------------------------------------------
    def _items(self, notes=None, connector_dir=None, sources=None, query=None) -> list[dict]:
        items = []
        for r in self.db.execute("SELECT ts,kind,detail,domain FROM activities").fetchall():
            items.append({"ts": r["ts"], "source": "activity", "kind": r["kind"],
                          "text": r["detail"], "domain": r["domain"]})
        for r in self.db.execute("SELECT ts,type,payload FROM events").fetchall():
            items.append({"ts": r["ts"], "source": "event", "kind": r["type"], "text": _short(r["payload"])})
        for r in self.db.execute("SELECT ts,title,status FROM decisions").fetchall():
            items.append({"ts": r["ts"], "source": "decision", "kind": "decision", "text": r["title"]})
        if notes:
            for n in notes:
                meta = n.meta or {}
                ts = meta.get("created") or meta.get("updated")
                if ts:
                    items.append({"ts": str(ts), "source": "note", "kind": "note", "text": n.title})
        if connector_dir:
            try:
                from ..connectors import ConnectorRegistry
                reg = ConnectorRegistry(connector_dir)
                for kind in ("email", "calendar", "tasks"):
                    for it in reg.list(kind, mode="private", limit=100):
                        if it.get("ts"):
                            items.append({"ts": str(it["ts"]), "source": kind, "kind": kind,
                                          "text": it.get("title", "")})
            except Exception:
                pass

        if sources:
            keep = set(sources)
            items = [i for i in items if i["source"] in keep]
        if query:
            terms = [w for w in query.lower().split() if len(w) > 1]
            items = [i for i in items if any(w in (i.get("text") or "").lower() for w in terms)]
        return [i for i in items if i.get("ts")]

    # --- chronological list -------------------------------------------------
    def build(self, notes=None, limit: int = 200, connector_dir=None, sources=None, query=None) -> list[dict]:
        items = self._items(notes, connector_dir, sources, query)
        items.sort(key=lambda x: x["ts"], reverse=True)
        return items[:limit]

    # --- grouping (day | week | month) -------------------------------------
    @staticmethod
    def _bucket(ts: str, period: str) -> str:
        ts = str(ts)
        if period == "month":
            return ts[:7]                      # YYYY-MM
        if period == "week":
            try:
                import datetime as _dt
                d = _dt.date.fromisoformat(ts[:10])
                y, w, _ = d.isocalendar()
                return f"{y}-W{w:02d}"
            except Exception:
                return ts[:10]
        return ts[:10]                          # day  YYYY-MM-DD

    def grouped(self, period: str = "day", **kw) -> list[dict]:
        items = self.build(limit=1000, **kw)
        buckets = defaultdict(list)
        for it in items:
            buckets[self._bucket(it["ts"], period)].append(it)
        out = [{"period": k, "count": len(v), "items": v} for k, v in buckets.items()]
        out.sort(key=lambda x: x["period"], reverse=True)
        return out

    # --- summary ------------------------------------------------------------
    def summary(self, **kw) -> dict:
        items = self.build(limit=2000, **kw)
        by_source = Counter(i["source"] for i in items)
        by_day = Counter(i["ts"][:10] for i in items)
        return {
            "total": len(items),
            "by_source": dict(by_source),
            "busiest_day": (by_day.most_common(1)[0] if by_day else None),
            "range": {"from": items[-1]["ts"] if items else None,
                      "to": items[0]["ts"] if items else None},
        }
