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

            CREATE TABLE IF NOT EXISTS learning_focuses (
                id         TEXT PRIMARY KEY,
                uid        TEXT NOT NULL,
                topic      TEXT NOT NULL,
                goal_id    TEXT,
                active     INTEGER DEFAULT 1,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS connector_sensor_seen (
                sensor    TEXT NOT NULL,
                item_key  TEXT NOT NULL,
                state     TEXT DEFAULT '',
                ts        TEXT,
                PRIMARY KEY (sensor, item_key)
            );

            CREATE TABLE IF NOT EXISTS connector_calls (
                id        TEXT PRIMARY KEY,
                uid       TEXT NOT NULL,
                connector TEXT NOT NULL,
                tool      TEXT,
                ok        INTEGER,
                ms        INTEGER,
                error     TEXT,
                ts        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS career_profile (
                uid              TEXT PRIMARY KEY,
                target_role      TEXT DEFAULT '',
                target_location  TEXT DEFAULT '',
                remote_ok        INTEGER DEFAULT 1,
                deadline         TEXT,
                resume_text_enc  TEXT,
                skills           TEXT DEFAULT '[]',
                updated_at       TEXT
            );

            CREATE TABLE IF NOT EXISTS job_postings (
                id            TEXT PRIMARY KEY,
                uid           TEXT NOT NULL,
                source        TEXT DEFAULT 'jobspy',
                title         TEXT DEFAULT '',
                company       TEXT DEFAULT '',
                url           TEXT DEFAULT '',
                location      TEXT DEFAULT '',
                salary        TEXT DEFAULT '',
                is_remote     INTEGER DEFAULT 0,
                description   TEXT DEFAULT '',
                keywords      TEXT DEFAULT '[]',
                match_score   REAL,
                match_factors TEXT DEFAULT '{}',
                status        TEXT DEFAULT 'discovered',
                discovered_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS applications (
                id           TEXT PRIMARY KEY,
                uid          TEXT NOT NULL,
                posting_id   TEXT NOT NULL,
                channel      TEXT DEFAULT '',
                status       TEXT DEFAULT 'prepared',
                match_score  REAL,
                ats_estimate REAL,
                draft        TEXT DEFAULT '',
                timeline     TEXT DEFAULT '[]',
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS company_intel (
                uid        TEXT NOT NULL,
                company    TEXT NOT NULL,
                notes      TEXT DEFAULT '',
                sources    TEXT DEFAULT '[]',
                cached_at  TEXT,
                PRIMARY KEY (uid, company)
            );

            CREATE TABLE IF NOT EXISTS life_metrics (
                uid                     TEXT NOT NULL,
                date                    TEXT NOT NULL,
                office_minutes          REAL,
                commute_out_minutes     REAL,
                commute_return_minutes  REAL,
                left_office_at          TEXT,
                gym_visits              INTEGER DEFAULT 0,
                home_arrival_at         TEXT,
                sleep_window_start      TEXT,
                sleep_window_end        TEXT,
                sleep_estimate_min      REAL,
                meals_out               INTEGER DEFAULT 0,
                late_night_orders       INTEGER DEFAULT 0,
                cafe_spend              REAL DEFAULT 0,
                meeting_count           INTEGER,
                meeting_minutes         REAL,
                focus_blocks            INTEGER,
                reading_minutes         REAL DEFAULT 0,
                late_night_activity_min REAL,
                meal_captures           INTEGER DEFAULT 0,
                meal_calorie_est        REAL,
                day_type                TEXT DEFAULT '',
                grace                   INTEGER DEFAULT 0,
                signal_counts           TEXT DEFAULT '{}',
                computed_at             TEXT,
                PRIMARY KEY (uid, date)
            );

            CREATE TABLE IF NOT EXISTS health_profile (
                uid              TEXT PRIMARY KEY,
                dob_or_age       TEXT DEFAULT '',
                sex              TEXT DEFAULT '',
                height_cm        REAL,
                weight_kg        REAL,
                activity_level   TEXT DEFAULT '',
                weight_log       TEXT DEFAULT '[]',
                constraints_enc  TEXT,
                provenance       TEXT DEFAULT '{}',
                updated_at       TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_runs_job   ON automation_runs(job_name, started_at);
            CREATE INDEX IF NOT EXISTS idx_feed_uid   ON learning_feed_items(uid, fetched_at);
            CREATE INDEX IF NOT EXISTS idx_appr_state ON approvals(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_llm_ts     ON llm_calls(ts);
            CREATE INDEX IF NOT EXISTS idx_focus_uid  ON learning_focuses(uid, active);
            CREATE INDEX IF NOT EXISTS idx_conncall   ON connector_calls(uid, connector, ts);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_posting_dedup ON job_postings(uid, url);
            CREATE INDEX IF NOT EXISTS idx_posting_uid ON job_postings(uid, status, discovered_at);
            CREATE INDEX IF NOT EXISTS idx_appl_uid    ON applications(uid, status);
            CREATE INDEX IF NOT EXISTS idx_appl_posting ON applications(posting_id);
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
        # Multi-focus upgrade: FK from an item to the learning_focuses row
        # that fetched it (focus_tag stays as the human-readable topic text)
        try:
            self.conn.execute(
                "ALTER TABLE learning_feed_items ADD COLUMN focus_id TEXT")
            self.conn.commit()
        except Exception:
            pass   # column already exists
        # CAREER AUTOPILOT Part 5D: thread_refs on applications — JSON
        # {"sent": [rfc2822 Message-IDs we sent], "seen": [inbound gmail msg
        # ids already processed]} so inbound replies can be thread-matched
        # and never double-processed across poll cycles.
        try:
            self.conn.execute(
                "ALTER TABLE applications ADD COLUMN thread_refs TEXT DEFAULT '{}'")
            self.conn.commit()
        except Exception:
            pass   # column already exists
        # CAREER AUTOPILOT Part 5E: cross-source posting dedup — the same job
        # discovered on a second board appends {source,url} here instead of
        # creating a second row (first-seen row wins).
        try:
            self.conn.execute(
                "ALTER TABLE job_postings ADD COLUMN alt_sources TEXT DEFAULT '[]'")
            self.conn.commit()
        except Exception:
            pass   # column already exists
        # LIFE AUTOPILOT L1: accepted targets (calorie/sleep/protein/water),
        # {kind: {value, unit, formula, accepted_at}} — populated only once
        # a proposal is approved, never on propose (propose don't impose).
        try:
            self.conn.execute(
                "ALTER TABLE health_profile ADD COLUMN targets TEXT DEFAULT '{}'")
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

    # --- connector call log (health tracking: Part 3 connectors tab + audit) --

    def log_connector_call(self, uid: str, connector: str, tool: str, ok: bool,
                           ms: int, error: str = ""):
        """Every outbound connector call (GitHub/Plane MCP, Google Calendar,
        ...) regardless of read/write or success/failure — feeds the
        connectors health tab (last successful call / last error per
        connector) and the audit report's external-write governance count."""
        self.conn.execute(
            "INSERT INTO connector_calls(id,uid,connector,tool,ok,ms,error,ts)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (_uuid(), uid, connector, tool, 1 if ok else 0, ms, (error or "")[:400],
             _now_iso()))
        self.conn.commit()

    def connector_status(self, connector: str | None = None) -> list[dict]:
        """Per-connector rollup: last successful call, last error, call
        counts — used by GET /api/connectors/status (Part 3)."""
        q = ("SELECT connector, COUNT(*) calls, SUM(ok) ok_calls,"
             " MAX(CASE WHEN ok=1 THEN ts END) last_ok_ts,"
             " MAX(CASE WHEN ok=0 THEN ts END) last_error_ts,"
             " (SELECT error FROM connector_calls c2 WHERE c2.connector=c1.connector"
             "  AND c2.ok=0 ORDER BY ts DESC LIMIT 1) last_error"
             " FROM connector_calls c1")
        args: list = []
        if connector:
            q += " WHERE connector=?"
            args.append(connector)
        q += " GROUP BY connector"
        rows = self.conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def recent_connector_calls(self, connector: str | None = None,
                               limit: int = 50) -> list[dict]:
        if connector:
            rows = self.conn.execute(
                "SELECT * FROM connector_calls WHERE connector=?"
                " ORDER BY ts DESC LIMIT ?", (connector, limit)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM connector_calls ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    # --- connector sensor diff state (Part 2: GitHubSensor/PlaneSensor) ------

    def sensor_seen_state(self, sensor: str, item_key: str) -> str | None:
        """None = never seen this item_key before (first sighting). Any
        other value (including "") is the last-known state string, so a
        sensor can tell "new item" apart from "item whose state changed"."""
        row = self.conn.execute(
            "SELECT state FROM connector_sensor_seen WHERE sensor=? AND item_key=?",
            (sensor, item_key)).fetchone()
        return None if row is None else (row["state"] or "")

    def mark_sensor_seen(self, sensor: str, item_key: str, state: str = "") -> None:
        self.conn.execute(
            "INSERT INTO connector_sensor_seen(sensor,item_key,state,ts)"
            " VALUES(?,?,?,?)"
            " ON CONFLICT(sensor,item_key) DO UPDATE SET state=excluded.state, ts=excluded.ts",
            (sensor, item_key, state, _now_iso()))
        self.conn.commit()

    # --- career profile (CAREER AUTOPILOT Part 1) ----------------------------
    # resume_text is sensitive (same class of data as GSTIN/PAN, see
    # CLAUDE.md) — stored Fernet-encrypted via amy.saas.security, the same
    # helper already used for stored API keys. Every LLM call that reads
    # resume_text back out must route sensitive=True.

    def get_career_profile(self, uid: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM career_profile WHERE uid=?", (uid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["skills"] = json.loads(d["skills"] or "[]")
        enc = d.pop("resume_text_enc", None)
        d["has_resume"] = bool(enc)
        if enc:
            try:
                from ..saas.security import decrypt_secret
                d["resume_text"] = decrypt_secret(enc)
            except Exception:
                d["resume_text"] = ""
        else:
            d["resume_text"] = ""
        return d

    def set_career_profile(self, uid: str, target_role: str | None = None,
                           target_location: str | None = None,
                           remote_ok: bool | None = None,
                           deadline: str | None = None,
                           resume_text: str | None = None,
                           skills: list | None = None) -> None:
        existing = self.conn.execute(
            "SELECT uid FROM career_profile WHERE uid=?", (uid,)).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO career_profile(uid,target_role,target_location,"
                " remote_ok,deadline,resume_text_enc,skills,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (uid, "", "", 1, None, None, "[]", _now_iso()))
        sets, args = [], []
        if target_role is not None:
            sets.append("target_role=?"); args.append(target_role)
        if target_location is not None:
            sets.append("target_location=?"); args.append(target_location)
        if remote_ok is not None:
            sets.append("remote_ok=?"); args.append(1 if remote_ok else 0)
        if deadline is not None:
            sets.append("deadline=?"); args.append(deadline)
        if skills is not None:
            sets.append("skills=?"); args.append(json.dumps(skills))
        if resume_text is not None:
            from ..saas.security import encrypt_secret
            sets.append("resume_text_enc=?")
            args.append(encrypt_secret(resume_text) if resume_text else None)
        sets.append("updated_at=?"); args.append(_now_iso())
        args.append(uid)
        self.conn.execute(f"UPDATE career_profile SET {', '.join(sets)} WHERE uid=?", args)
        self.conn.commit()

    # --- life metrics (LIFE AUTOPILOT L2) --------------------------------------

    _LIFE_METRICS_COLS = (
        "office_minutes", "commute_out_minutes", "commute_return_minutes",
        "left_office_at", "gym_visits", "home_arrival_at", "sleep_window_start",
        "sleep_window_end", "sleep_estimate_min", "meals_out", "late_night_orders",
        "cafe_spend", "meeting_count", "meeting_minutes", "focus_blocks",
        "reading_minutes", "late_night_activity_min", "meal_captures",
        "meal_calorie_est", "day_type", "grace", "signal_counts")

    def get_life_metrics(self, uid: str, date: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM life_metrics WHERE uid=? AND date=?", (uid, date)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["grace"] = bool(d["grace"])
        d["signal_counts"] = json.loads(d.get("signal_counts") or "{}")
        return d

    def list_life_metrics(self, uid: str, since: str, until: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM life_metrics WHERE uid=? AND date>=? AND date<=?"
            " ORDER BY date", (uid, since, until)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["grace"] = bool(d["grace"])
            d["signal_counts"] = json.loads(d.get("signal_counts") or "{}")
            out.append(d)
        return out

    def upsert_life_metrics(self, uid: str, date: str, **fields) -> None:
        """Idempotent recompute — always UPSERTs the full row rather than
        insert-only, so re-running life_metrics_daily/backfill for an
        already-computed day overwrites cleanly instead of erroring or
        duplicating."""
        cols = [c for c in self._LIFE_METRICS_COLS if c in fields]
        values = []
        for c in cols:
            v = fields[c]
            if c == "grace":
                v = 1 if v else 0
            elif c == "signal_counts":
                v = json.dumps(v or {})
            values.append(v)
        col_list = ", ".join(cols)
        placeholders = ", ".join("?" for _ in cols)
        update_clause = ", ".join(f"{c}=excluded.{c}" for c in cols)
        self.conn.execute(
            f"INSERT INTO life_metrics(uid,date,{col_list},computed_at)"
            f" VALUES(?,?,{placeholders},?)"
            f" ON CONFLICT(uid,date) DO UPDATE SET {update_clause}, computed_at=excluded.computed_at",
            [uid, date, *values, _now_iso()])
        self.conn.commit()

    # --- health profile (LIFE AUTOPILOT L1) -----------------------------------

    def get_health_profile(self, uid: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM health_profile WHERE uid=?", (uid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["weight_log"] = json.loads(d["weight_log"] or "[]")
        d["provenance"] = json.loads(d["provenance"] or "{}")
        d["targets"] = json.loads(d.get("targets") or "{}")
        enc = d.pop("constraints_enc", None)
        if enc:
            try:
                from ..saas.security import decrypt_secret
                d["constraints"] = decrypt_secret(enc)
            except Exception:
                d["constraints"] = ""
        else:
            d["constraints"] = ""
        from ..life.targets import resolve_age
        d["age"] = resolve_age(d.get("dob_or_age") or "")
        return d

    def upsert_health_profile(self, uid: str, dob_or_age: str | None = None,
                              sex: str | None = None, height_cm: float | None = None,
                              weight_kg: float | None = None,
                              activity_level: str | None = None,
                              constraints: str | None = None,
                              provenance: dict | None = None) -> None:
        """provenance: partial dict of {field: 'vault'|'manual'} merged into
        the stored provenance map (never wholesale-replaced) so a later
        manual edit of one field doesn't erase another field's vault
        provenance."""
        existing = self.conn.execute(
            "SELECT uid, provenance FROM health_profile WHERE uid=?", (uid,)).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO health_profile(uid,dob_or_age,sex,height_cm,weight_kg,"
                " activity_level,weight_log,constraints_enc,provenance,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (uid, "", "", None, None, "", "[]", None, "{}", _now_iso()))
            prov_current = {}
        else:
            prov_current = json.loads(existing["provenance"] or "{}")
        sets, args = [], []
        if dob_or_age is not None:
            sets.append("dob_or_age=?"); args.append(dob_or_age)
        if sex is not None:
            sets.append("sex=?"); args.append(sex)
        if height_cm is not None:
            sets.append("height_cm=?"); args.append(height_cm)
        if weight_kg is not None:
            sets.append("weight_kg=?"); args.append(weight_kg)
        if activity_level is not None:
            sets.append("activity_level=?"); args.append(activity_level)
        if constraints is not None:
            from ..saas.security import encrypt_secret
            sets.append("constraints_enc=?")
            args.append(encrypt_secret(constraints) if constraints else None)
        if provenance:
            prov_current.update(provenance)
            sets.append("provenance=?"); args.append(json.dumps(prov_current))
        sets.append("updated_at=?"); args.append(_now_iso())
        args.append(uid)
        self.conn.execute(f"UPDATE health_profile SET {', '.join(sets)} WHERE uid=?", args)
        self.conn.commit()

    def append_weight_log(self, uid: str, weight_kg: float,
                          date: str | None = None, source: str = "manual") -> dict:
        """Appends to weight_log and updates the current weight_kg. Returns
        {previous_weight_kg, weight_kg, pct_change} so the caller can decide
        whether a >5% shift warrants a target re-proposal (never silent)."""
        profile = self.get_health_profile(uid) or {}
        previous = profile.get("weight_kg")
        log = list(profile.get("weight_log") or [])
        log.append({"date": date or _now_iso()[:10], "weight_kg": weight_kg, "source": source})
        self.upsert_health_profile(uid, weight_kg=weight_kg)
        self.conn.execute(
            "UPDATE health_profile SET weight_log=? WHERE uid=?",
            (json.dumps(log), uid))
        self.conn.commit()
        pct_change = ((weight_kg - previous) / previous * 100.0) if previous else None
        return {"previous_weight_kg": previous, "weight_kg": weight_kg,
               "pct_change": round(pct_change, 2) if pct_change is not None else None}

    def set_health_target(self, uid: str, kind: str, value) -> None:
        """Merges one accepted target (kind -> value) into health_profile's
        targets JSON — called only by the health_target_propose executor on
        approval, never on propose."""
        row = self.conn.execute(
            "SELECT targets FROM health_profile WHERE uid=?", (uid,)).fetchone()
        current = json.loads(row["targets"] or "{}") if row else {}
        current[kind] = {"value": value, "accepted_at": _now_iso()}
        self.conn.execute(
            "UPDATE health_profile SET targets=? WHERE uid=?",
            (json.dumps(current), uid))
        self.conn.commit()

    # --- job postings (CAREER AUTOPILOT Part 1) -------------------------------

    @staticmethod
    def _posting_fuzzy_key(title: str, company: str, location: str) -> str:
        """Normalized title+company+location — the Part 5E cross-source dedup
        key. Lowercase, non-alphanumerics collapsed to single spaces, so
        'Sr. ML Engineer — Acme, Bangalore' from two boards normalizes the
        same regardless of each board's punctuation habits."""
        import re as _re
        raw = f"{title} {company} {location}".lower()
        return _re.sub(r"[^a-z0-9]+", " ", raw).strip()

    def add_posting_if_new(self, uid: str, posting: dict) -> tuple[str, bool]:
        """Dedup on (uid, url), then fuzzy on normalized title+company+
        location (CAREER AUTOPILOT Part 5E: the same job cross-posted to a
        second board must not become a second row/event). A fuzzy hit keeps
        the FIRST-seen row and appends this sighting's {source, url} to its
        alt_sources. Returns (posting_id, is_new)."""
        url = (posting.get("url") or "").strip()
        if url:
            row = self.conn.execute(
                "SELECT id FROM job_postings WHERE uid=? AND url=?", (uid, url)).fetchone()
            if row:
                return row["id"], False
        fuzzy_key = self._posting_fuzzy_key(posting.get("title") or "",
                                            posting.get("company") or "",
                                            posting.get("location") or "")
        if fuzzy_key:
            for row in self.conn.execute(
                    "SELECT id, title, company, location, alt_sources"
                    " FROM job_postings WHERE uid=?", (uid,)).fetchall():
                if self._posting_fuzzy_key(row["title"], row["company"],
                                           row["location"]) != fuzzy_key:
                    continue
                alt = json.loads(row["alt_sources"] or "[]")
                sighting = {"source": posting.get("source") or "jobspy", "url": url}
                if sighting not in alt:
                    alt.append(sighting)
                    self.conn.execute(
                        "UPDATE job_postings SET alt_sources=? WHERE uid=? AND id=?",
                        (json.dumps(alt), uid, row["id"]))
                    self.conn.commit()
                return row["id"], False
        pid = _uuid()
        self.conn.execute(
            "INSERT INTO job_postings(id,uid,source,title,company,url,location,"
            " salary,is_remote,description,keywords,status,discovered_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, uid, posting.get("source") or "jobspy",
             posting.get("title") or "", posting.get("company") or "", url,
             posting.get("location") or "", str(posting.get("salary") or ""),
             1 if posting.get("is_remote") else 0, posting.get("description") or "",
             json.dumps(posting.get("keywords") or []), "discovered", _now_iso()))
        self.conn.commit()
        return pid, True

    def get_posting(self, uid: str, posting_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM job_postings WHERE uid=? AND id=?", (uid, posting_id)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["keywords"] = json.loads(d["keywords"] or "[]")
        d["match_factors"] = json.loads(d["match_factors"] or "{}")
        d["alt_sources"] = json.loads(d.get("alt_sources") or "[]")
        d["sources_count"] = 1 + len(d["alt_sources"])
        return d

    def list_postings(self, uid: str, status: str | None = None,
                      limit: int = 50) -> list[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM job_postings WHERE uid=? AND status=?"
                " ORDER BY COALESCE(match_score,-1) DESC, discovered_at DESC LIMIT ?",
                (uid, status, limit)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM job_postings WHERE uid=?"
                " ORDER BY COALESCE(match_score,-1) DESC, discovered_at DESC LIMIT ?",
                (uid, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["keywords"] = json.loads(d["keywords"] or "[]")
            d["match_factors"] = json.loads(d["match_factors"] or "{}")
            d["alt_sources"] = json.loads(d.get("alt_sources") or "[]")
            d["sources_count"] = 1 + len(d["alt_sources"])
            out.append(d)
        return out

    def set_posting_match(self, uid: str, posting_id: str, match_score: float,
                          match_factors: dict) -> bool:
        cur = self.conn.execute(
            "UPDATE job_postings SET match_score=?, match_factors=? WHERE uid=? AND id=?",
            (match_score, json.dumps(match_factors), uid, posting_id))
        self.conn.commit()
        return cur.rowcount > 0

    def set_posting_status(self, uid: str, posting_id: str, status: str) -> bool:
        cur = self.conn.execute(
            "UPDATE job_postings SET status=? WHERE uid=? AND id=?",
            (status, uid, posting_id))
        self.conn.commit()
        return cur.rowcount > 0

    # --- applications (CAREER AUTOPILOT Part 1) --------------------------------

    # "accepted" (Part 5E) is the new terminal success status — an accepted
    # offer triggers the goal wind-down proposal (agents/reactive.py).
    _APPLICATION_STATUSES = ("prepared", "approved", "sent", "response",
                             "interview", "offer", "accepted", "rejected",
                             "ghosted")

    def create_application(self, uid: str, posting_id: str, channel: str = "",
                           match_score: float | None = None,
                           ats_estimate: float | None = None,
                           draft: str = "", note: str = "") -> str:
        aid = _uuid()
        now = _now_iso()
        timeline = [{"ts": now, "status": "prepared", "note": note}]
        self.conn.execute(
            "INSERT INTO applications(id,uid,posting_id,channel,status,"
            " match_score,ats_estimate,draft,timeline,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (aid, uid, posting_id, channel, "prepared", match_score,
             ats_estimate, draft, json.dumps(timeline), now, now))
        self.conn.commit()
        return aid

    def get_application(self, uid: str, application_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM applications WHERE uid=? AND id=?",
            (uid, application_id)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["timeline"] = json.loads(d["timeline"] or "[]")
        d["thread_refs"] = json.loads(d.get("thread_refs") or "{}")
        return d

    def add_application_thread_ref(self, uid: str, application_id: str,
                                   kind: str, ref: str) -> bool:
        """Part 5D: record either a sent Message-ID (kind='sent') or an
        already-processed inbound gmail message id (kind='seen') on the
        application, so replies thread-match and never double-process."""
        row = self.conn.execute(
            "SELECT thread_refs FROM applications WHERE uid=? AND id=?",
            (uid, application_id)).fetchone()
        if row is None:
            return False
        refs = json.loads(row["thread_refs"] or "{}")
        bucket = refs.setdefault(kind, [])
        if ref not in bucket:
            bucket.append(ref)
        self.conn.execute(
            "UPDATE applications SET thread_refs=? WHERE uid=? AND id=?",
            (json.dumps(refs), uid, application_id))
        self.conn.commit()
        return True

    def list_applications(self, uid: str, status: str | None = None) -> list[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM applications WHERE uid=? AND status=?"
                " ORDER BY updated_at DESC", (uid, status)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM applications WHERE uid=? ORDER BY updated_at DESC",
                (uid,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["timeline"] = json.loads(d["timeline"] or "[]")
            d["thread_refs"] = json.loads(d.get("thread_refs") or "{}")
            out.append(d)
        return out

    def update_application_status(self, uid: str, application_id: str,
                                  status: str, note: str = "") -> bool:
        if status not in self._APPLICATION_STATUSES:
            raise ValueError(f"unknown application status {status!r}")
        row = self.conn.execute(
            "SELECT timeline FROM applications WHERE uid=? AND id=?",
            (uid, application_id)).fetchone()
        if row is None:
            return False
        timeline = json.loads(row["timeline"] or "[]")
        timeline.append({"ts": _now_iso(), "status": status, "note": note})
        self.conn.execute(
            "UPDATE applications SET status=?, timeline=?, updated_at=?"
            " WHERE uid=? AND id=?",
            (status, json.dumps(timeline), _now_iso(), uid, application_id))
        self.conn.commit()
        return True

    def career_funnel_counts(self, uid: str) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) n FROM applications WHERE uid=? GROUP BY status",
            (uid,)).fetchall()
        counts = {s: 0 for s in self._APPLICATION_STATUSES}
        counts.update({r["status"]: r["n"] for r in rows})
        counts["discovered"] = self.conn.execute(
            "SELECT COUNT(*) n FROM job_postings WHERE uid=?", (uid,)).fetchone()["n"]
        return counts

    # --- company intel (CAREER AUTOPILOT Part 1) -------------------------------

    def upsert_company_intel(self, uid: str, company: str, notes: str,
                             sources: list[str]) -> None:
        self.conn.execute(
            "INSERT INTO company_intel(uid,company,notes,sources,cached_at)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(uid,company) DO UPDATE SET"
            "  notes=excluded.notes, sources=excluded.sources, cached_at=excluded.cached_at",
            (uid, company, notes, json.dumps(sources), _now_iso()))
        self.conn.commit()

    def get_company_intel(self, uid: str, company: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM company_intel WHERE uid=? AND company=?",
            (uid, company)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["sources"] = json.loads(d["sources"] or "[]")
        return d


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
