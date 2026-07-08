"""LearningFeedSensor — pulls the user's learning feed through promoted MCP
connectors, ranks it against their focus topic, and lands the result in
collab.db + the vault (same Sensor base as GmailSensor).

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

from ..operational.sensors import Sensor
from ..events.store import LEARNING_FEED_REFRESHED
from . import aggregator, ranker

log = logging.getLogger("amy.learning_feed")

TOP_SAVE = 30      # rows upserted into learning_feed_items per refresh
TOP_NOTE = 5       # items in the daily vault note

DEFAULT_FOCUS = "generative AI"
FOCUS_PREF_KEY = "learning_focus"


def resolve_focus(collab_conn) -> str:
    """prefs['learning_focus'] → env AMY_LEARNING_FOCUS → default."""
    try:
        row = collab_conn.execute(
            "SELECT value FROM prefs WHERE key=?", (FOCUS_PREF_KEY,)).fetchone()
        if row and str(row["value"]).strip():
            return str(row["value"]).strip()
    except Exception:
        pass
    from .. import config
    return config._env("AMY_LEARNING_FOCUS", DEFAULT_FOCUS).strip() or DEFAULT_FOCUS


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

    def poll(self, focus: str | None = None) -> dict:
        """Fetch → rank → upsert → event → vault note. Synchronous like every
        other sensor; safe to asyncio.run() here because callers (automation
        tick, FastAPI BackgroundTasks) run it in a worker thread."""
        # lazy table creation lives in AutomationStore._init — make sure it ran
        from ..automation.store import AutomationStore
        AutomationStore(self.collab)

        focus = (focus or resolve_focus(self.collab.conn)).strip() or DEFAULT_FOCUS

        if not self.connector_rows:
            log.warning("learning_feed: no promoted learning-feed connectors for user %s "
                        "— register one via /api/mcp/connectors", self.user_id)
            return {"focus": focus, "connectors": 0, "fetched": 0, "saved": 0,
                    "skipped": "no promoted learning-feed connectors"}

        items = asyncio.run(aggregator.fetch_all(focus, self.connector_rows))
        if not items:
            return {"focus": focus, "connectors": len(self.connector_rows),
                    "fetched": 0, "saved": 0, "skipped": "no items returned"}

        items = ranker.rank(items, focus, self.llm)
        top = items[:TOP_SAVE]
        self._upsert(top, focus)
        self._emit(top, focus)
        note = self._write_note(top, focus)

        return {"focus": focus, "connectors": len(self.connector_rows),
                "fetched": len(items), "saved": len(top),
                "sources": sorted({it["source"] for it in top}),
                "ranked": any(it.get("relevance") is not None for it in top),
                "note": note}

    # --- internals -----------------------------------------------------

    def _upsert(self, items: list[dict], focus: str):
        """ON CONFLICT upsert keyed on the deterministic URL-hash id, so a
        re-fetch refreshes scores WITHOUT clobbering the user's saved flag."""
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self.collab.conn.executemany(
            "INSERT INTO learning_feed_items"
            " (id,uid,source,title,url,summary,score,relevance,why,focus_tag,"
            "  saved,fetched_at,published_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)"
            " ON CONFLICT(id) DO UPDATE SET"
            "  source=excluded.source, title=excluded.title,"
            "  summary=excluded.summary, score=excluded.score,"
            "  relevance=excluded.relevance, why=excluded.why,"
            "  focus_tag=excluded.focus_tag, fetched_at=excluded.fetched_at,"
            "  published_at=excluded.published_at",
            [(it["id"], self.user_id, it["source"], it["title"], it["url"],
              it["summary"], it["score"], it.get("relevance"), it.get("why", ""),
              focus, now, it.get("published_at")) for it in items])
        # light retention: unsaved rows older than 30 days age out
        cutoff = (_dt.datetime.now(_dt.timezone.utc)
                  - _dt.timedelta(days=30)).isoformat()
        self.collab.conn.execute(
            "DELETE FROM learning_feed_items WHERE uid=? AND saved=0 AND fetched_at<?",
            (self.user_id, cutoff))
        self.collab.conn.commit()

    def _emit(self, items: list[dict], focus: str):
        try:
            self.publish(LEARNING_FEED_REFRESHED, {
                "focus": focus,
                "items": len(items),
                "sources": sorted({it["source"] for it in items}),
                "top_title": items[0]["title"] if items else "",
            })
        except Exception:
            pass   # fire-and-forget, same stance as _emit_fin in finance.py

    def _write_note(self, items: list[dict], focus: str) -> str | None:
        """09_Memory/Learning feed - YYYY-MM-DD.md via MemoryWriter (top 5),
        idempotent on eid so repeat runs the same day don't duplicate."""
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
                "learning feed", today, "\n".join(lines),
                eid=f"learningfeed-{today}", tags=["learning", "feed"])
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
    turned off, so the handler must re-check."""
    if not _enabled():
        return {"skipped": "AMY_LEARNING_FEED_ENABLED is not true"}
    rows = promoted_learning_rows(ctx.user_id)
    sensor = LearningFeedSensor(ctx.events(), ctx.collab, ctx.user_id,
                                llm=ctx.llm, connector_rows=rows)
    return sensor.poll()


def refresh_for_user(user_id: str, focus: str | None = None) -> dict:
    """Self-contained refresh for FastAPI BackgroundTasks (threadpool —
    asyncio.run inside poll() is safe there). Opens and closes its own
    CollabDB; mirrors build_ctx's LLM setup including the per-user
    local-only routing pref."""
    from ..collab import CollabDB
    from ..events.store import EventStore
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
        rows = promoted_learning_rows(user_id)
        sensor = LearningFeedSensor(EventStore(cdb), cdb, user_id,
                                    llm=llm, connector_rows=rows)
        return sensor.poll(focus=focus)
    except Exception as exc:
        log.warning("learning_feed: background refresh failed for %s: %s", user_id, exc)
        return {"error": str(exc)[:300]}
    finally:
        cdb.close()
