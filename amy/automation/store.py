"""Automation backbone — durable jobs, run ledger, Approval Inbox, LLM call log.

All state lives in the user's collab.db (same file the event bus uses) so the
whole automation layer stays per-user and inspectable with one sqlite file.

Autonomy tiers (the heart of the hybrid model):
  tier 0 — auto:           executed immediately, recorded, no notification.
  tier 1 — auto + notify:  executed immediately + an in-app notification.
  tier 2 — ask first:      parked as status='pending' until the user approves.

Schedules are small JSON blobs, one of:
  {"every_hours": 6}
  {"daily_at": "07:00"}
  {"monthly_day": 1, "at": "06:00"}
"""
from __future__ import annotations

import datetime as _dt
import json
import time
import uuid


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _uuid() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Schedule math (local time — cadences are about the user's day, not UTC)
# ---------------------------------------------------------------------------

def compute_next_run(schedule: dict, after: _dt.datetime | None = None) -> str:
    """Return the next local-time ISO timestamp this schedule should fire."""
    now = after or _dt.datetime.now()
    if "every_hours" in schedule:
        return (now + _dt.timedelta(hours=float(schedule["every_hours"]))).isoformat(timespec="seconds")

    at = schedule.get("at") or schedule.get("daily_at") or "06:00"
    hh, mm = (int(p) for p in str(at).split(":")[:2])

    if "monthly_day" in schedule:
        day = int(schedule["monthly_day"])
        candidate = now.replace(day=1, hour=hh, minute=mm, second=0, microsecond=0)
        candidate = candidate.replace(day=min(day, 28))
        if candidate <= now:
            # first of next month, then apply the day
            nxt = (candidate.replace(day=1) + _dt.timedelta(days=32)).replace(day=1)
            candidate = nxt.replace(day=min(day, 28))
        return candidate.isoformat(timespec="seconds")

    # daily_at
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= now:
        candidate += _dt.timedelta(days=1)
    return candidate.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class AutomationStore:
    """Jobs + runs + approvals + llm call log, all in collab.db."""

    PAUSE_PREF = "automation_paused"

    def __init__(self, collab_db):
        self.db = collab_db
        self.conn = collab_db.conn
        self._init()

    def _init(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS automation_jobs (
                name        TEXT PRIMARY KEY,
                schedule    TEXT NOT NULL,
                enabled     INTEGER DEFAULT 1,
                last_run_at TEXT,
                next_run_at TEXT,
                last_status TEXT,
                config      TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS automation_runs (
                id          TEXT PRIMARY KEY,
                job_name    TEXT NOT NULL,
                started_at  TEXT NOT NULL,
                finished_at TEXT,
                status      TEXT DEFAULT 'running',
                detail      TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS approvals (
                id          TEXT PRIMARY KEY,
                created_at  TEXT NOT NULL,
                decided_at  TEXT,
                tier        INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                title       TEXT NOT NULL,
                body        TEXT DEFAULT '',
                payload     TEXT DEFAULT '{}',
                status      TEXT DEFAULT 'pending',
                result      TEXT,
                source      TEXT DEFAULT '',
                dedup_key   TEXT
            );

            CREATE TABLE IF NOT EXISTS llm_calls (
                id       TEXT PRIMARY KEY,
                ts       TEXT NOT NULL,
                provider TEXT,
                purpose  TEXT,
                ok       INTEGER,
                ms       INTEGER,
                error    TEXT
            );

            CREATE TABLE IF NOT EXISTS learning_feed_items (
                id           TEXT PRIMARY KEY,
                uid          TEXT NOT NULL,
                source       TEXT,
                title        TEXT,
                url          TEXT,
                summary      TEXT,
                score        INTEGER,
                relevance    REAL,
                why          TEXT,
                focus_tag    TEXT,
                saved        INTEGER DEFAULT 0,
                fetched_at   TEXT,
                published_at TEXT
            );

            CREATE TABLE IF NOT EXISTS ingested_attachments (
                msg_id   TEXT NOT NULL,
                filename TEXT NOT NULL,
                sha256   TEXT,
                status   TEXT,
                ts       TEXT,
                detail   TEXT DEFAULT '',
                PRIMARY KEY (msg_id, filename)
            );

            CREATE INDEX IF NOT EXISTS idx_runs_job   ON automation_runs(job_name, started_at);
            CREATE INDEX IF NOT EXISTS idx_feed_uid   ON learning_feed_items(uid, fetched_at);
            CREATE INDEX IF NOT EXISTS idx_appr_state ON approvals(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_llm_ts     ON llm_calls(ts);
            """
        )
        self.conn.commit()
        # Idempotent column upgrades (R3): reasoning/risk/affected_entity/expires_at
        for col, decl in (("reasoning", "TEXT DEFAULT ''"),
                          ("risk", "TEXT DEFAULT ''"),
                          ("affected_entity", "TEXT DEFAULT ''"),
                          ("expires_at", "TEXT")):
            try:
                self.conn.execute(f"ALTER TABLE approvals ADD COLUMN {col} {decl}")
                self.conn.commit()
            except Exception:
                pass   # column already exists
        # Learning feed watch-progress upgrade (videos: resume + completion)
        for col, decl in (("progress", "REAL DEFAULT 0"),
                          ("position_sec", "INTEGER DEFAULT 0"),
                          ("duration_sec", "INTEGER"),
                          ("completed_at", "TEXT")):
            try:
                self.conn.execute(
                    f"ALTER TABLE learning_feed_items ADD COLUMN {col} {decl}")
                self.conn.commit()
            except Exception:
                pass   # column already exists

    # --- global pause -------------------------------------------------------

    def paused(self) -> bool:
        row = self.conn.execute(
            "SELECT value FROM prefs WHERE key=?", (self.PAUSE_PREF,)).fetchone()
        return bool(row and row["value"] == "1")

    def set_paused(self, paused: bool):
        self.conn.execute(
            "INSERT INTO prefs(key,value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (self.PAUSE_PREF, "1" if paused else "0"))
        self.conn.commit()

    # --- jobs ----------------------------------------------------------------

    def ensure_job(self, name: str, schedule: dict, enabled: bool = True):
        """Insert the job if missing; never overrides user edits to an existing one."""
        row = self.conn.execute(
            "SELECT name FROM automation_jobs WHERE name=?", (name,)).fetchone()
        if row:
            return
        self.conn.execute(
            "INSERT INTO automation_jobs(name,schedule,enabled,next_run_at) VALUES(?,?,?,?)",
            (name, json.dumps(schedule), 1 if enabled else 0, compute_next_run(schedule)))
        self.conn.commit()

    def list_jobs(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM automation_jobs ORDER BY name").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["schedule"] = json.loads(d["schedule"] or "{}")
            d["config"] = json.loads(d["config"] or "{}")
            d["enabled"] = bool(d["enabled"])
            out.append(d)
        return out

    def get_job(self, name: str) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM automation_jobs WHERE name=?", (name,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["schedule"] = json.loads(d["schedule"] or "{}")
        d["config"] = json.loads(d["config"] or "{}")
        d["enabled"] = bool(d["enabled"])
        return d

    def update_job(self, name: str, enabled: bool | None = None,
                   schedule: dict | None = None) -> bool:
        job = self.get_job(name)
        if not job:
            return False
        if enabled is not None:
            self.conn.execute(
                "UPDATE automation_jobs SET enabled=? WHERE name=?",
                (1 if enabled else 0, name))
        if schedule is not None:
            self.conn.execute(
                "UPDATE automation_jobs SET schedule=?, next_run_at=? WHERE name=?",
                (json.dumps(schedule), compute_next_run(schedule), name))
        self.conn.commit()
        return True

    def due_jobs(self, now: _dt.datetime | None = None) -> list[dict]:
        now_s = (now or _dt.datetime.now()).isoformat(timespec="seconds")
        return [j for j in self.list_jobs()
                if j["enabled"] and (j["next_run_at"] or "") <= now_s]

    # --- run ledger ----------------------------------------------------------

    def start_run(self, job_name: str) -> str:
        rid = _uuid()
        self.conn.execute(
            "INSERT INTO automation_runs(id,job_name,started_at) VALUES(?,?,?)",
            (rid, job_name, _now_iso()))
        self.conn.commit()
        return rid

    def finish_run(self, rid: str, status: str, detail: dict | None = None):
        self.conn.execute(
            "UPDATE automation_runs SET finished_at=?, status=?, detail=? WHERE id=?",
            (_now_iso(), status, json.dumps(detail or {}), rid))
        self.conn.commit()

    def mark_job_ran(self, name: str, status: str):
        job = self.get_job(name)
        if not job:
            return
        self.conn.execute(
            "UPDATE automation_jobs SET last_run_at=?, last_status=?, next_run_at=? WHERE name=?",
            (_now_iso(), status, compute_next_run(job["schedule"]), name))
        self.conn.commit()

    def list_runs(self, job_name: str | None = None, limit: int = 50) -> list[dict]:
        if job_name:
            rows = self.conn.execute(
                "SELECT * FROM automation_runs WHERE job_name=?"
                " ORDER BY started_at DESC LIMIT ?", (job_name, limit)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM automation_runs ORDER BY started_at DESC LIMIT ?",
                (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"] or "{}")
            out.append(d)
        return out

    # --- approvals -----------------------------------------------------------

    def create_approval(self, tier: int, action_type: str, title: str,
                        body: str = "", payload: dict | None = None,
                        source: str = "", status: str = "pending",
                        dedup_key: str | None = None,
                        result: dict | None = None,
                        reasoning: str = "", risk: str = "",
                        affected_entity: str = "",
                        expires_at: str | None = None) -> str | None:
        """Insert an approval row. Returns None if an open row with the same
        dedup_key already exists (so daily jobs don't re-propose the same thing)."""
        if dedup_key:
            row = self.conn.execute(
                "SELECT id FROM approvals WHERE dedup_key=?"
                " AND status IN ('pending','executed','auto_executed') LIMIT 1",
                (dedup_key,)).fetchone()
            if row:
                return None
        aid = _uuid()
        self.conn.execute(
            "INSERT INTO approvals(id,created_at,tier,action_type,title,body,"
            " payload,status,source,dedup_key,result,reasoning,risk,"
            " affected_entity,expires_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, _now_iso(), tier, action_type, title, body,
             json.dumps(payload or {}), status, source, dedup_key,
             json.dumps(result) if result is not None else None,
             reasoning, risk, affected_entity, expires_at))
        self.conn.commit()
        return aid

    def expire_stale(self) -> int:
        """Mark pending approvals past their expires_at as expired, and
        clear their 'approval needed' bell notifications (an expired item
        is no longer actionable — same reasoning as approve()/reject()
        clearing theirs, so the badge never gets permanently stuck)."""
        now = _now_iso()
        expired_ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM approvals"
            " WHERE status='pending' AND expires_at IS NOT NULL AND expires_at<?",
            (now,)).fetchall()]
        if not expired_ids:
            return 0
        self.conn.executemany(
            "UPDATE approvals SET status='expired', decided_at=? WHERE id=?",
            [(now, aid) for aid in expired_ids])
        self.conn.commit()
        try:
            from ..notifications import NotificationStore
            ns = NotificationStore(self.db)
            for aid in expired_ids:
                ns.mark_read_by_related_id(aid)
        except Exception:
            pass   # notification cleanup is best-effort; expiry itself already committed
        return len(expired_ids)

    def get_approval(self, aid: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["payload"] = json.loads(d["payload"] or "{}")
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return d

    def list_approvals(self, status: str | None = "pending", limit: int = 100) -> list[dict]:
        try:
            self.expire_stale()
        except Exception:
            pass   # listing must still work even if the expiry sweep fails
        if status:
            rows = self.conn.execute(
                "SELECT * FROM approvals WHERE status=?"
                " ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"] or "{}")
            d["result"] = json.loads(d["result"]) if d["result"] else None
            out.append(d)
        return out

    def set_approval_status(self, aid: str, status: str, result: dict | None = None):
        self.conn.execute(
            "UPDATE approvals SET status=?, decided_at=?, result=? WHERE id=?",
            (status, _now_iso(),
             json.dumps(result) if result is not None else None, aid))
        self.conn.commit()

    def pending_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) n FROM approvals WHERE status='pending'").fetchone()["n"]

    # --- ingested attachments -----------------------------------------------

    def attachment_seen(self, msg_id: str, filename: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM ingested_attachments WHERE msg_id=? AND filename=?",
            (msg_id, filename)).fetchone() is not None

    def mark_attachment(self, msg_id: str, filename: str, sha256: str,
                        status: str, detail: str = ""):
        self.conn.execute(
            "INSERT OR REPLACE INTO ingested_attachments(msg_id,filename,sha256,status,ts,detail)"
            " VALUES(?,?,?,?,?,?)",
            (msg_id, filename, sha256, status, _now_iso(), detail))
        self.conn.commit()

    # --- llm call log ---------------------------------------------------------

    def log_llm_call(self, provider: str, purpose: str, ok: bool,
                     ms: int, error: str = ""):
        self.conn.execute(
            "INSERT INTO llm_calls(id,ts,provider,purpose,ok,ms,error) VALUES(?,?,?,?,?,?,?)",
            (_uuid(), _now_iso(), provider, purpose, 1 if ok else 0, ms, error[:400]))
        self.conn.commit()

    def llm_stats(self, hours: int = 168) -> dict:
        cutoff = (_dt.datetime.now(_dt.timezone.utc)
                  - _dt.timedelta(hours=hours)).isoformat()
        rows = self.conn.execute(
            "SELECT provider, COUNT(*) calls, SUM(ok) ok_calls,"
            " AVG(ms) avg_ms FROM llm_calls WHERE ts>=? GROUP BY provider",
            (cutoff,)).fetchall()
        return {"window_hours": hours,
                "providers": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# TrackedLLM — drop-in LLMRouter wrapper that logs every call
# ---------------------------------------------------------------------------

class TrackedLLM:
    """Wraps an LLMRouter; same generate() contract, logs to llm_calls.

    Safe to hand to any existing code that calls llm.generate(...) — it
    returns the same (text, provider_name) tuple.

    force_local=True implements the per-user "local-only" routing flag:
    EVERY call is made with sensitive=True, so LLMRouter.pick() only ever
    returns Ollama (or the offline template) — no cloud provider sees the
    user's data regardless of per-call sensitivity classification.
    """

    def __init__(self, router, store: AutomationStore, purpose: str = "automation",
                 force_local: bool = False):
        self._router = router
        self._store = store
        self.purpose = purpose
        self.force_local = force_local

    def pick(self, sensitive: bool):
        return self._router.pick(sensitive or self.force_local)

    def status(self) -> dict:
        return self._router.status()

    def generate(self, system, prompt, context="", sensitive=False, fast=False):
        t0 = time.monotonic()
        try:
            text, name = self._router.generate(
                system, prompt, context,
                sensitive=sensitive or self.force_local, fast=fast)
            ms = int((time.monotonic() - t0) * 1000)
            try:
                self._store.log_llm_call(name, self.purpose, True, ms)
            except Exception:
                pass
            return text, name
        except Exception as exc:
            ms = int((time.monotonic() - t0) * 1000)
            try:
                self._store.log_llm_call("unknown", self.purpose, False, ms, str(exc))
            except Exception:
                pass
            raise
