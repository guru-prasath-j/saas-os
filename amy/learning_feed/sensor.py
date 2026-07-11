"""LearningFeedSensor — pulls the user's learning feed through promoted MCP
connectors, ranks it against the user's focus topics, and lands the result
in collab.db + the vault (same Sensor base as GmailSensor).

Multi-focus: a user can track several topics at once (`learning_focuses`
table), each optionally linked to a Goal. poll_one() handles a single focus
row; poll_all() loops every active focus for the user. poll() is a
back-compat single-topic entry point used when refreshing just one focus
by name (e.g. right after the user edits it).

Scheduling is NOT a startup loop: the learning_feed_refresh job handler at
the bottom is registered in amy/automation/jobs.py ({"every_hours": 6},
same cadence mechanism as gmail_statement_ingest), so runs show up in the
automation run ledger and can be triggered manually via
POST /api/automation/jobs/learning_feed_refresh/run.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import uuid

from ..operational.sensors import Sensor
from ..events.store import LEARNING_FEED_REFRESHED
from . import aggregator, ranker

log = logging.getLogger("amy.learning_feed")

TOP_SAVE = 30      # rows upserted into learning_feed_items per refresh
TOP_NOTE = 5       # items in the daily vault note

DEFAULT_FOCUS = "generative AI"
FOCUS_PREF_KEY = "learning_focus"


def resolve_focus(collab_conn) -> str:
    """prefs['learning_focus'] → env AMY_LEARNING_FOCUS → default. Legacy
    single-topic resolver — used only to seed a user's first focus row."""
    try:
        row = collab_conn.execute(
            "SELECT value FROM prefs WHERE key=?", (FOCUS_PREF_KEY,)).fetchone()
        if row and str(row["value"]).strip():
            return str(row["value"]).strip()
    except Exception:
        pass
    from .. import config
    return config._env("AMY_LEARNING_FOCUS", DEFAULT_FOCUS).strip() or DEFAULT_FOCUS


# ---------------------------------------------------------------------------
# learning_focuses CRUD (table created by AutomationStore._init)
# ---------------------------------------------------------------------------

def list_focuses(collab_conn, uid: str) -> list[dict]:
    """Active focuses for this user. A first-time user (no rows yet) gets
    one seeded from the legacy single-focus resolver, so today's setup
    keeps working with zero action required."""
    rows = collab_conn.execute(
        "SELECT * FROM learning_focuses WHERE uid=? AND active=1"
        " ORDER BY created_at", (uid,)).fetchall()
    if rows:
        return [dict(r) for r in rows]
    fid = add_focus(collab_conn, uid, resolve_focus(collab_conn))
    row = collab_conn.execute(
        "SELECT * FROM learning_focuses WHERE id=?", (fid,)).fetchone()
    return [dict(row)] if row else []


def add_focus(collab_conn, uid: str, topic: str, goal_id: str | None = None) -> str:
    fid = uuid.uuid4().hex[:16]
    collab_conn.execute(
        "INSERT INTO learning_focuses(id,uid,topic,goal_id,active,created_at)"
        " VALUES(?,?,?,?,1,?)",
        (fid, uid, topic.strip()[:200], goal_id,
         _dt.datetime.now(_dt.timezone.utc).isoformat()))
    collab_conn.commit()
    return fid


def set_focus_active(collab_conn, uid: str, focus_id: str, active: bool) -> bool:
    cur = collab_conn.execute(
        "UPDATE learning_focuses SET active=? WHERE id=? AND uid=?",
        (1 if active else 0, focus_id, uid))
    collab_conn.commit()
    return cur.rowcount > 0


def set_focus_goal(collab_conn, uid: str, focus_id: str, goal_id: str | None) -> bool:
    cur = collab_conn.execute(
        "UPDATE learning_focuses SET goal_id=? WHERE id=? AND uid=?",
        (goal_id, focus_id, uid))
    collab_conn.commit()
    return cur.rowcount > 0


def delete_focus(collab_conn, uid: str, focus_id: str) -> bool:
    cur = collab_conn.execute(
        "DELETE FROM learning_focuses WHERE id=? AND uid=?", (focus_id, uid))
    collab_conn.commit()
    return cur.rowcount > 0


def add_manual_item(collab_conn, uid: str, title: str, note: str = "",
                    focus_id: str | None = None, url: str = "") -> str:
    """Manual learning capture ("I learned X today") — same
    learning_feed_items row shape the auto-fetched pipeline uses (source=
    'manual'), so it shows up in the Learn tab feed, the dashboard card,
    and the activity-log trend engine identically to an aggregator hit.
    Landed pre-completed (saved=1, progress=1.0, completed_at=now): unlike
    an aggregator item the user hasn't seen yet, a manual entry describes
    something already done."""
    iid = uuid.uuid4().hex[:16]
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    focus_tag = None
    if focus_id:
        row = collab_conn.execute(
            "SELECT topic FROM learning_focuses WHERE id=? AND uid=?",
            (focus_id, uid)).fetchone()
        focus_tag = row["topic"] if row else None
    collab_conn.execute(
        "INSERT INTO learning_feed_items"
        " (id,uid,source,title,url,summary,score,relevance,why,focus_tag,focus_id,"
        "  saved,fetched_at,published_at,progress,completed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,1.0,?)",
        (iid, uid, "manual", title.strip()[:300], url.strip()[:500] or None,
         note.strip()[:2000] or None, None, None, None, focus_tag, focus_id,
         now, now, now))
    collab_conn.commit()
    return iid


def promoted_learning_rows(user_id: str) -> list:
    """McpConnector rows (amy_saas.db) promoted to sensor AND matching a
    known learning-feed source name. Column attrs are loaded before the
    session closes (same detached-row pattern as app.py's _run_mcp_polls)."""
    from ..saas.db import SessionLocal, McpConnector
    s = SessionLocal()
    try:
        rows = s.query(McpConnector).filter(
            McpConnector.user_id == user_id,
            McpConnector.promoted_to_sensor == True).all()  # noqa: E712
    finally:
        s.close()
    return [r for r in rows if aggregator.tool_for(r.name)]


class LearningFeedSensor(Sensor):
    name = "learning_feed"

    def __init__(self, event_store, collab_db, user_id: str,
                 llm=None, connector_rows: list | None = None):
        super().__init__(event_store)
        self.collab = collab_db
        self.user_id = user_id
        self.llm = llm
        self.connector_rows = connector_rows or []

    def poll_one(self, focus_row: dict) -> dict:
        """Fetch → rank → upsert → event → vault note for ONE focus row
        ({id, topic, goal_id, ...}). Synchronous like every other sensor;
        safe to asyncio.run() here because callers (automation tick,
        FastAPI BackgroundTasks) run it in a worker thread."""
        from ..automation.store import AutomationStore
        AutomationStore(self.collab)

        topic = (focus_row.get("topic") or "").strip() or DEFAULT_FOCUS
        focus_id = focus_row.get("id")

        if not self.connector_rows:
            log.warning("learning_feed: no promoted learning-feed connectors for user %s "
                        "— register one via /api/mcp/connectors", self.user_id)
            return {"focus": topic, "focus_id": focus_id, "connectors": 0,
                    "fetched": 0, "saved": 0,
                    "skipped": "no promoted learning-feed connectors"}

        items = asyncio.run(aggregator.fetch_all(topic, self.connector_rows))
        if not items:
            return {"focus": topic, "focus_id": focus_id,
                    "connectors": len(self.connector_rows),
                    "fetched": 0, "saved": 0, "skipped": "no items returned"}

        items = ranker.rank(items, topic, self.llm)
        top = items[:TOP_SAVE]
        # Source-fairness floor (COURSES SOURCE): high-volume feeds (HN/
        # Dev.to return ~20 each) can push a smaller source's entire result
        # set below the save cap — found live: the courses source fetched 12
        # items and saved 0. Any source with zero rows in the top slice gets
        # its best 3 swapped in for the lowest-ranked overflow.
        represented = {it.get("source") for it in top}
        floor: list[dict] = []
        for src in {it.get("source") for it in items} - represented:
            floor.extend([it for it in items if it.get("source") == src][:3])
        if floor:
            top = top[:max(0, TOP_SAVE - len(floor))] + floor
        self._upsert(top, topic, focus_id)
        self._emit(top, topic, focus_id)
        note = self._write_note(top, topic, focus_id)

        return {"focus": topic, "focus_id": focus_id,
                "connectors": len(self.connector_rows),
                "fetched": len(items), "saved": len(top),
                "sources": sorted({it["source"] for it in top}),
                "ranked": any(it.get("relevance") is not None for it in top),
                "note": note}

    def poll_all(self) -> dict:
        """Poll every active focus for this user. One failing focus (e.g.
        an LLM hiccup) never blocks the others."""
        from ..automation.store import AutomationStore
        AutomationStore(self.collab)

        focuses = list_focuses(self.collab.conn, self.user_id)
        results = []
        for row in focuses:
            try:
                results.append(self.poll_one(row))
            except Exception as exc:
                log.warning("learning_feed: focus %r failed: %s", row.get("topic"), exc)
                results.append({"focus": row.get("topic"), "focus_id": row.get("id"),
                                "error": str(exc)[:200]})
        return {"focuses": results,
                "saved_total": sum(r.get("saved", 0) for r in results)}

    def poll(self, focus: str | None = None) -> dict:
        """Back-compat single-topic entry point (the router's 'save &
        refresh' after editing one focus): resolves/creates the matching
        learning_focuses row by topic text and polls just that one."""
        from ..automation.store import AutomationStore
        AutomationStore(self.collab)

        topic = (focus or resolve_focus(self.collab.conn)).strip() or DEFAULT_FOCUS
        row = self.collab.conn.execute(
            "SELECT * FROM learning_focuses WHERE uid=? AND topic=?",
            (self.user_id, topic)).fetchone()
        if row is None:
            fid = add_focus(self.collab.conn, self.user_id, topic)
            row = self.collab.conn.execute(
                "SELECT * FROM learning_focuses WHERE id=?", (fid,)).fetchone()
        return self.poll_one(dict(row))

    # --- internals -----------------------------------------------------

    def _upsert(self, items: list[dict], focus: str, focus_id: str | None = None):
        """ON CONFLICT upsert keyed on the deterministic URL-hash id, so a
        re-fetch refreshes scores WITHOUT clobbering the user's saved flag."""
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self.collab.conn.executemany(
            "INSERT INTO learning_feed_items"
            " (id,uid,source,title,url,summary,score,relevance,why,focus_tag,focus_id,"
            "  saved,fetched_at,published_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?)"
            " ON CONFLICT(id) DO UPDATE SET"
            "  source=excluded.source, title=excluded.title,"
            "  summary=excluded.summary, score=excluded.score,"
            "  relevance=excluded.relevance, why=excluded.why,"
            "  focus_tag=excluded.focus_tag, focus_id=excluded.focus_id,"
            "  fetched_at=excluded.fetched_at,"
            "  published_at=excluded.published_at",
            [(it["id"], self.user_id, it["source"], it["title"], it["url"],
              it["summary"], it["score"], it.get("relevance"), it.get("why", ""),
              focus, focus_id, now, it.get("published_at")) for it in items])
        # light retention: unsaved rows older than 30 days age out
        cutoff = (_dt.datetime.now(_dt.timezone.utc)
                  - _dt.timedelta(days=30)).isoformat()
        self.collab.conn.execute(
            "DELETE FROM learning_feed_items WHERE uid=? AND saved=0 AND fetched_at<?",
            (self.user_id, cutoff))
        self.collab.conn.commit()

    def _emit(self, items: list[dict], focus: str, focus_id: str | None = None):
        try:
            self.publish(LEARNING_FEED_REFRESHED, {
                "focus": focus,
                "focus_id": focus_id,
                "items": len(items),
                "sources": sorted({it["source"] for it in items}),
                "top_title": items[0]["title"] if items else "",
            })
        except Exception:
            pass   # fire-and-forget, same stance as _emit_fin in finance.py

    def _write_note(self, items: list[dict], focus: str,
                    focus_id: str | None = None) -> str | None:
        """09_Memory/Learning feed - YYYY-MM-DD - <focus>.md via MemoryWriter
        (top 5), idempotent on eid so repeat runs the same day for the same
        focus don't duplicate. The focus suffix keeps multiple focuses
        refreshing the same day from colliding on one note."""
        try:
            from ..memory.writer import MemoryWriter
            from ..saas import tenancy
            vault = tenancy.resolve_vault_dir(self.user_id)
            if not vault.exists():
                return None
            today = _dt.date.today().isoformat()
            lines = [f"Focus: **{focus}**", ""]
            for it in items[:TOP_NOTE]:
                why = f" — {it['why']}" if it.get("why") else ""
                rel = f" ({it['relevance']:.0f}/10)" if it.get("relevance") is not None else ""
                lines.append(f"- [{it['title']}]({it['url']}) `{it['source']}`{rel}{why}")
            p = MemoryWriter(vault).write_atomic(
                "learning feed", f"{today} - {focus}", "\n".join(lines),
                eid=f"learningfeed-{today}-{focus_id or focus}",
                tags=["learning", "feed"])
            return str(p) if p else "already-written"
        except Exception as exc:
            log.warning("learning_feed: vault note failed: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Entry points — automation job + router fire-and-forget share this path
# ---------------------------------------------------------------------------

def _enabled() -> bool:
    from .. import config
    return config._env("AMY_LEARNING_FEED_ENABLED", "false").strip().lower() == "true"


def learning_feed_refresh(ctx) -> dict:
    """Automation job handler (JobCtx). Env-gated here as well as at job
    registration: job rows persist in automation_jobs after the env flag is
    turned off, so the handler must re-check. Refreshes every active focus."""
    if not _enabled():
        return {"skipped": "AMY_LEARNING_FEED_ENABLED is not true"}
    rows = promoted_learning_rows(ctx.user_id)
    sensor = LearningFeedSensor(ctx.events(), ctx.collab, ctx.user_id,
                                llm=ctx.llm, connector_rows=rows)
    return sensor.poll_all()


def refresh_for_user(user_id: str, focus: str | None = None,
                     focus_id: str | None = None) -> dict:
    """Self-contained refresh for FastAPI BackgroundTasks (threadpool —
    asyncio.run inside poll() is safe there). Opens and closes its own
    CollabDB; mirrors build_ctx's LLM setup including the per-user
    local-only routing pref.

    With `focus_id` set, refreshes exactly that row and no-ops if it's
    since been deleted — this is what create/reactivate should pass,
    since resolving by topic TEXT (the `focus` kwarg) would otherwise
    silently recreate a focus a user deleted in the seconds before this
    queued task runs. `focus` (topic text) is a legacy single-topic
    fallback kept for callers with no row id in hand. Neither set:
    refreshes every active focus."""
    from ..collab import CollabDB
    from ..llm import LLMRouter
    from ..saas import paths
    from ..automation.store import AutomationStore, TrackedLLM

    cdb = CollabDB(str(paths.index_dir(user_id) / "collab.db"))
    try:
        store = AutomationStore(cdb)
        llm = None
        try:
            local_only = False
            row = cdb.conn.execute(
                "SELECT value FROM prefs WHERE key='llm_local_only'").fetchone()
            local_only = bool(row and str(row["value"]) == "1")
            llm = TrackedLLM(LLMRouter(use_global_keys=True), store,
                             purpose="learning_feed", force_local=local_only)
        except Exception:
            llm = None
        # amy.events.factory.get_events() (Part 0 / quirk 20 fix) wires
        # reactive agents onto THIS EventStore instance so a background-task
        # refresh reacts too, not just the job path; degrades to a bare
        # store on any wiring failure (the refresh itself must still run).
        from ..events.factory import get_events
        events = get_events(user_id, cdb, index_dir=paths.index_dir(user_id))
        rows = promoted_learning_rows(user_id)
        sensor = LearningFeedSensor(events, cdb, user_id,
                                    llm=llm, connector_rows=rows)
        if focus_id:
            row = cdb.conn.execute(
                "SELECT * FROM learning_focuses WHERE id=? AND uid=?",
                (focus_id, user_id)).fetchone()
            if row is None:
                return {"skipped": "focus no longer exists"}
            return sensor.poll_one(dict(row))
        if focus:
            return sensor.poll(focus=focus)
        return sensor.poll_all()
    except Exception as exc:
        log.warning("learning_feed: background refresh failed for %s: %s", user_id, exc)
        return {"error": str(exc)[:300]}
    finally:
        cdb.close()
