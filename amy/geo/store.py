"""Geo context store (C1) — places, enter/leave visits, last-fix state.

Location is the first *physical world* sensor (docs/CONTEXT_PLAN.md). A phone
or browser posts coordinates; ingest_location() matches them against saved
places and turns raw pings into place-level transitions the event bus can
carry. Privacy rails: raw coordinates are stored only as the single last-fix
row in geo_state — history is visits (place-level), and coordinates are never
sent to any LLM.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import uuid

# Hysteresis: enter at d <= radius, leave only at d > radius * LEAVE_FACTOR —
# a GPS fix wobbling around the boundary must not churn enter/leave events.
LEAVE_FACTOR = 1.3

# Unmatched fixes are kept only as day-level counts on a ~110 m grid cell
# (3-decimal rounding), pruned after CELL_RETENTION_DAYS — enough for the C2
# place-learning correlator, deliberately too coarse to reconstruct movement.
CELL_DECIMALS = 3
CELL_RETENTION_DAYS = 60


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def cell_key(lat: float, lon: float) -> str:
    return f"{round(lat, CELL_DECIMALS):.{CELL_DECIMALS}f},{round(lon, CELL_DECIMALS):.{CELL_DECIMALS}f}"


def cell_center(cell: str) -> tuple[float, float]:
    lat, lon = cell.split(",")
    return float(lat), float(lon)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class GeoStore:
    def __init__(self, collab_db):
        self.db = collab_db
        self.db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS geo_places (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT DEFAULT '',
                lat REAL NOT NULL, lon REAL NOT NULL,
                radius_m INTEGER DEFAULT 150,
                source TEXT DEFAULT 'manual',
                meta TEXT DEFAULT '{}', created_at TEXT);
            CREATE TABLE IF NOT EXISTS geo_visits (
                id TEXT PRIMARY KEY, place_id TEXT NOT NULL,
                entered_at TEXT, left_at TEXT);
            CREATE TABLE IF NOT EXISTS geo_state (
                key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS geo_cells (
                cell TEXT NOT NULL, day TEXT NOT NULL,
                hits INTEGER DEFAULT 1,
                PRIMARY KEY (cell, day));
        """)
        self.db.conn.commit()

    # --- places ------------------------------------------------------------
    def add_place(self, name: str, lat: float, lon: float, kind: str = "",
                  radius_m: int = 150, source: str = "manual",
                  meta: dict | None = None) -> str:
        pid = uuid.uuid4().hex[:12]
        self.db.conn.execute(
            "INSERT INTO geo_places (id,name,kind,lat,lon,radius_m,source,meta,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, name.strip(), kind.strip().lower(), float(lat), float(lon),
             int(radius_m), source, json.dumps(meta or {}), _now()))
        self.db.conn.commit()
        return pid

    def list_places(self) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM geo_places ORDER BY created_at").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["meta"] = json.loads(d.get("meta") or "{}")
            out.append(d)
        return out

    def get_place(self, pid: str) -> dict | None:
        r = self.db.conn.execute(
            "SELECT * FROM geo_places WHERE id=?", (pid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["meta"] = json.loads(d.get("meta") or "{}")
        return d

    def update_place(self, pid: str, **fields) -> bool:
        allowed = {"name", "kind", "lat", "lon", "radius_m", "meta"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "meta":
                v = json.dumps(v)
            if k == "kind":
                v = str(v).strip().lower()
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return False
        vals.append(pid)
        c = self.db.conn.execute(
            f"UPDATE geo_places SET {', '.join(sets)} WHERE id=?", vals)
        self.db.conn.commit()
        return c.rowcount > 0

    def delete_place(self, pid: str) -> bool:
        c = self.db.conn.execute("DELETE FROM geo_places WHERE id=?", (pid,))
        self.db.conn.execute(
            "DELETE FROM geo_visits WHERE place_id=?", (pid,))
        self.db.conn.commit()
        return c.rowcount > 0

    # --- visits / state ------------------------------------------------------
    def open_visits(self) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT v.*, p.name, p.kind FROM geo_visits v"
            " JOIN geo_places p ON p.id = v.place_id"
            " WHERE v.left_at IS NULL").fetchall()
        return [dict(r) for r in rows]

    def recent_visits(self, limit: int = 30) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT v.*, p.name, p.kind FROM geo_visits v"
            " JOIN geo_places p ON p.id = v.place_id"
            " ORDER BY v.entered_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def last_fix(self) -> dict | None:
        r = self.db.conn.execute(
            "SELECT value FROM geo_state WHERE key='last_fix'").fetchone()
        return json.loads(r["value"]) if r else None

    # --- the sensor inlet ----------------------------------------------------
    def ingest_location(self, lat: float, lon: float, accuracy_m: float = 0.0,
                        ts: str | None = None, source: str = "phone") -> dict:
        """Match a fix against places, open/close visits, save last fix.

        Returns {"entered": [place…], "left": [place…], "inside": [place…]}
        where entered/left are the *transitions* this fix caused.
        """
        # cell days must align with transaction dates, which are LOCAL — an
        # evening visit in IST is "tomorrow" in UTC and would never correlate
        day = ts[:10] if ts else _dt.date.today().isoformat()
        ts = ts or _now()
        lat, lon = float(lat), float(lon)
        open_by_place = {v["place_id"]: v for v in self.open_visits()}
        entered, left, inside = [], [], []

        for p in self.list_places():
            d = haversine_m(lat, lon, p["lat"], p["lon"])
            radius = max(30, int(p["radius_m"] or 150))
            was_inside = p["id"] in open_by_place
            if not was_inside and d <= radius:
                self.db.conn.execute(
                    "INSERT INTO geo_visits (id,place_id,entered_at) VALUES (?,?,?)",
                    (uuid.uuid4().hex[:12], p["id"], ts))
                entered.append(p)
                inside.append(p)
            elif was_inside and d > radius * LEAVE_FACTOR:
                self.db.conn.execute(
                    "UPDATE geo_visits SET left_at=? WHERE id=?",
                    (ts, open_by_place[p["id"]]["id"]))
                left.append(p)
            elif was_inside:
                inside.append(p)

        if not inside:
            # unmatched fix → coarse day-level cell count (place-learning C2)
            self.db.conn.execute(
                "INSERT INTO geo_cells (cell, day, hits) VALUES (?,?,1)"
                " ON CONFLICT(cell, day) DO UPDATE SET hits = hits + 1",
                (cell_key(lat, lon), day))
            cutoff = (_dt.date.fromisoformat(day)
                      - _dt.timedelta(days=CELL_RETENTION_DAYS)).isoformat()
            self.db.conn.execute("DELETE FROM geo_cells WHERE day < ?", (cutoff,))

        self.db.conn.execute(
            "INSERT INTO geo_state (key,value) VALUES ('last_fix',?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps({"lat": lat, "lon": lon, "accuracy_m": accuracy_m,
                         "ts": ts, "source": source}),))
        self.db.conn.commit()
        return {"entered": entered, "left": left, "inside": inside}

    def cell_days(self, min_days: int = 2) -> dict[str, set[str]]:
        """cell → set of days it was visited, for cells seen on ≥ min_days days."""
        rows = self.db.conn.execute(
            "SELECT cell, day FROM geo_cells").fetchall()
        by_cell: dict[str, set[str]] = {}
        for r in rows:
            by_cell.setdefault(r["cell"], set()).add(r["day"])
        return {c: d for c, d in by_cell.items() if len(d) >= min_days}
