"""Context sensor endpoints (/api/context) — CONTEXT_PLAN C1.

The location inlet any client can post to (Flutter geofence transitions,
browser navigator.geolocation, curl). Ingest matches the fix against saved
places, opens/closes visits, and emits context.place_entered / _left onto the
event bus with reactive agents wired — the errand agent reacts synchronously,
so the notification exists by the time the POST returns.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import User
from ..deps import current_user, _collab_db_path
from .. import paths

router = APIRouter()


def _open_collab(user: User):
    from ...collab import CollabDB
    return CollabDB(_collab_db_path(user))


def _events_with_agents(user: User, cdb):
    """EventStore with reactive agents attached, via amy.events.factory
    (Part 0 / quirk 20 fix) — the bus is per-instance, so agents must be
    wired where the emit happens. Wiring failures degrade to a bare store;
    the event itself must still emit."""
    from ...events.factory import get_events
    return get_events(user.id, cdb, index_dir=paths.index_dir(user.id),
                      user_email=user.email)


def _place_public(p: dict) -> dict:
    """Event payloads carry place identity, never coordinates (privacy rail)."""
    return {"place_id": p["id"], "name": p["name"], "kind": p.get("kind") or ""}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LocationBody(BaseModel):
    lat: float
    lon: float
    accuracy_m: float = 0.0
    ts: str | None = None
    source: str = "phone"


class PlaceBody(BaseModel):
    name: str
    lat: float
    lon: float
    kind: str = ""
    radius_m: int = 150


class PlacePatch(BaseModel):
    name: str | None = None
    kind: str | None = None
    lat: float | None = None
    lon: float | None = None
    radius_m: int | None = None


class PlaceTagBody(BaseModel):
    place_tag: str


# ---------------------------------------------------------------------------
# Location inlet
# ---------------------------------------------------------------------------

@router.post("/api/context/location")
def post_location(body: LocationBody, user: User = Depends(current_user)):
    from ...geo import GeoStore
    cdb = _open_collab(user)
    try:
        gs = GeoStore(cdb)
        result = gs.ingest_location(body.lat, body.lon, body.accuracy_m,
                                    body.ts, body.source)
        if result["entered"] or result["left"]:
            es = _events_with_agents(user, cdb)
            for p in result["entered"]:
                es.emit("context.place_entered", _place_public(p), source="geo")
            for p in result["left"]:
                es.emit("context.place_left", _place_public(p), source="geo")
            es.emit("context.location_updated",
                    {"inside": [_place_public(p) for p in result["inside"]],
                     "source": body.source}, source="geo")
        return {"entered": [_place_public(p) for p in result["entered"]],
                "left": [_place_public(p) for p in result["left"]],
                "inside": [_place_public(p) for p in result["inside"]]}
    finally:
        cdb.close()


@router.get("/api/context/status")
def context_status(user: User = Depends(current_user)):
    from ...geo import GeoStore
    cdb = _open_collab(user)
    try:
        gs = GeoStore(cdb)
        return {"last_fix": gs.last_fix(),
                "inside": [{"place_id": v["place_id"], "name": v["name"],
                            "kind": v["kind"], "entered_at": v["entered_at"]}
                           for v in gs.open_visits()],
                "places": len(gs.list_places())}
    finally:
        cdb.close()


@router.get("/api/context/visits")
def list_visits(limit: int = 30, user: User = Depends(current_user)):
    from ...geo import GeoStore
    cdb = _open_collab(user)
    try:
        return {"visits": GeoStore(cdb).recent_visits(limit)}
    finally:
        cdb.close()


# ---------------------------------------------------------------------------
# Places CRUD
# ---------------------------------------------------------------------------

@router.post("/api/context/places")
def add_place(body: PlaceBody, user: User = Depends(current_user)):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    from ...geo import GeoStore
    cdb = _open_collab(user)
    try:
        pid = GeoStore(cdb).add_place(body.name, body.lat, body.lon,
                                      kind=body.kind, radius_m=body.radius_m)
        return {"id": pid}
    finally:
        cdb.close()


@router.get("/api/context/places")
def list_places(user: User = Depends(current_user)):
    from ...geo import GeoStore
    cdb = _open_collab(user)
    try:
        return {"places": GeoStore(cdb).list_places()}
    finally:
        cdb.close()


@router.patch("/api/context/places/{pid}")
def update_place(pid: str, body: PlacePatch, user: User = Depends(current_user)):
    from ...geo import GeoStore
    cdb = _open_collab(user)
    try:
        ok = GeoStore(cdb).update_place(pid, **body.model_dump())
        if not ok:
            raise HTTPException(status_code=404, detail="place not found or no fields")
        return {"ok": True}
    finally:
        cdb.close()


@router.delete("/api/context/places/{pid}")
def delete_place(pid: str, user: User = Depends(current_user)):
    from ...geo import GeoStore
    cdb = _open_collab(user)
    try:
        if not GeoStore(cdb).delete_place(pid):
            raise HTTPException(status_code=404, detail="place not found")
        return {"ok": True}
    finally:
        cdb.close()


# ---------------------------------------------------------------------------
# Task place-tagging (errand match key)
# ---------------------------------------------------------------------------

@router.patch("/api/context/tasks/{tid}/place-tag")
def set_task_place_tag(tid: str, body: PlaceTagBody,
                       user: User = Depends(current_user)):
    cdb = _open_collab(user)
    try:
        c = cdb.conn.execute(
            "UPDATE tasks SET place_tag=? WHERE id=?",
            (body.place_tag.strip().lower(), tid))
        cdb.conn.commit()
        if c.rowcount == 0:
            raise HTTPException(status_code=404, detail="task not found")
        return {"ok": True}
    finally:
        cdb.close()
