"""GitHubSensor / PlaneSensor (CONNECTOR COMPLETION Part 2).

Same Sensor pattern as amy/finance/sync/gmail_sensor.py: wraps an existing
read path (here, amy/connectors/mcp_call.call_mcp_tool against the user's
registered GitHub/Plane MCP connectors — see amy/tools/connector_tools.py's
read tools for the same transport) and emits canonical events through the
injected EventStore.

Distinct from the legacy Operational-Layer amy/sensors/github_sensor.py
(env-token GitHub API, webhook + /events feed) — this sensor is MCP-based
and polling-only, feeding github.pr_review_requested/pr_status_changed/
issue_assigned (not amy/sensors/github_models.py's github.NEW_* types).

Diffing: amy/automation/store.py's connector_sensor_seen table tracks a
per-item "seen" key so a repeat poll of unchanged data emits nothing (poll
is idempotent) — sensor_seen_state() returns None for "never seen" (so a
brand new item still fires once) vs. any other string for "seen, and this
was its last known state" (so a _STATUS_CHANGED event only fires on an
actual transition, never on first sighting — there's nothing to have
"changed" from yet).

Field extraction is deliberately tolerant (candidate key lists, like
amy/learning_feed/aggregator.py) since neither the official GitHub MCP
server's nor Plane's exact response shape is something this codebase can
pin down without a live server; if a field isn't where expected the sensor
degrades to skipping that item rather than raising.

Known limitation: "assigned to me" / "review requested of me" isn't
filtered against the authenticated identity (that would need a stable
get_me-equivalent call this codebase can't verify against a live server) —
today ANY item with a non-empty reviewers/assignees list counts. Fine for a
single-user-per-connector deployment (this Amy install); revisit if a
connector is ever shared.

Payloads carry ids/titles/urls/states only — no tokens, no full diffs or
bodies (CONNECTOR COMPLETION Part 2 constraint).
"""
from __future__ import annotations

from ..events.store import (
    GITHUB_PR_REVIEW_REQUESTED, GITHUB_PR_STATUS_CHANGED, GITHUB_ISSUE_ASSIGNED,
    PLANE_TASK_ASSIGNED, PLANE_TASK_DUE_SOON, PLANE_TASK_STATUS_CHANGED,
)
from ..operational.sensors import Sensor


def _first(d: dict, keys: tuple[str, ...]):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


_ID_KEYS = ("number", "pr_number", "id")
_TITLE_KEYS = ("title", "name")
_URL_KEYS = ("html_url", "url", "link")
_STATE_KEYS = ("state", "status", "review_decision", "mergeable_state")
_REPO_KEYS = ("repository", "repo", "repo_full_name")


class GitHubSensor(Sensor):
    name = "github_connector"

    def __init__(self, event_store, ctx):
        super().__init__(event_store)
        self.ctx = ctx

    def poll(self) -> list[dict]:
        """One poll cycle: open PRs (review-requested + status-changed) then
        open issues (assigned). Any connector failure degrades to []
        (never raises — the caller is a 15-minute job tick)."""
        from .mcp_call import ConnectorCallError, call_mcp_tool, extract_list, find_connector_row

        emitted: list[dict] = []
        row = find_connector_row(self.ctx.user_id, "github")
        if row is None:
            return emitted
        repo_default = (row.default_target or "").strip()
        store = self.ctx.store

        try:
            prs = call_mcp_tool(self.ctx.user_id, store, "github",
                                ("list_pull_requests", "search_pull_requests"),
                                {"state": "open"})
        except ConnectorCallError:
            prs = None
        if prs is not None:
            for pr in extract_list(prs):
                number = _first(pr, _ID_KEYS)
                if number is None:
                    continue
                repo = _first(pr, _REPO_KEYS) or repo_default
                title = _first(pr, _TITLE_KEYS) or f"PR #{number}"
                url = _first(pr, _URL_KEYS) or ""
                reviewers = pr.get("requested_reviewers") or pr.get("reviewers") or []
                state = str(_first(pr, _STATE_KEYS) or "").lower()

                if reviewers:
                    key = f"pr_review_{repo}_{number}"
                    if store.sensor_seen_state("github_pr_review", key) is None:
                        payload = {"repo": repo, "number": number, "title": title, "url": url}
                        self.publish(GITHUB_PR_REVIEW_REQUESTED, payload)
                        store.mark_sensor_seen("github_pr_review", key, "seen")
                        emitted.append(payload)

                if state:
                    status_key = f"pr_status_{repo}_{number}"
                    last_state = store.sensor_seen_state("github_pr_status", status_key)
                    if last_state is not None and state != last_state:
                        payload = {"repo": repo, "number": number, "title": title,
                                  "url": url, "state": state}
                        self.publish(GITHUB_PR_STATUS_CHANGED, payload)
                        emitted.append(payload)
                    if state != last_state:
                        store.mark_sensor_seen("github_pr_status", status_key, state)

        try:
            issues = call_mcp_tool(self.ctx.user_id, store, "github",
                                   ("list_issues", "search_issues"), {"state": "open"})
        except ConnectorCallError:
            issues = None
        if issues is not None:
            for iss in extract_list(issues):
                if iss.get("pull_request"):
                    continue   # GitHub's REST /issues also lists PRs — skip, PRs are handled above
                number = _first(iss, _ID_KEYS)
                if number is None:
                    continue
                assignees = iss.get("assignees") or ([iss["assignee"]] if iss.get("assignee") else [])
                if not assignees:
                    continue
                repo = _first(iss, _REPO_KEYS) or repo_default
                key = f"issue_assigned_{repo}_{number}"
                if store.sensor_seen_state("github_issue_assigned", key) is None:
                    title = _first(iss, _TITLE_KEYS) or f"Issue #{number}"
                    url = _first(iss, _URL_KEYS) or ""
                    payload = {"repo": repo, "number": number, "title": title, "url": url}
                    self.publish(GITHUB_ISSUE_ASSIGNED, payload)
                    store.mark_sensor_seen("github_issue_assigned", key, "seen")
                    emitted.append(payload)

        return emitted


_TASK_ID_KEYS = ("id", "task_id")
_DUE_KEYS = ("due_date", "target_date")


class PlaneSensor(Sensor):
    name = "plane_connector"

    def __init__(self, event_store, ctx, due_soon_hours: int = 48):
        super().__init__(event_store)
        self.ctx = ctx
        self.due_soon_hours = due_soon_hours

    def poll(self) -> list[dict]:
        import datetime as _dt
        from .mcp_call import ConnectorCallError, call_mcp_tool, extract_list, find_connector_row

        emitted: list[dict] = []
        row = find_connector_row(self.ctx.user_id, "plane")
        if row is None:
            return emitted
        store = self.ctx.store

        try:
            compacted = call_mcp_tool(self.ctx.user_id, store, "plane",
                                      ("list_work_items", "list_issues", "get_issues"),
                                      {}, target_style="single")
        except ConnectorCallError:
            return emitted

        cutoff = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=self.due_soon_hours)
        for task in extract_list(compacted):
            tid = _first(task, _TASK_ID_KEYS)
            if tid is None:
                continue
            title = _first(task, _TITLE_KEYS) or f"Task {tid}"
            url = _first(task, _URL_KEYS) or ""
            assignees = task.get("assignees") or ([task["assignee"]] if task.get("assignee") else [])
            state = str(_first(task, _STATE_KEYS) or "").lower()
            due = _first(task, _DUE_KEYS)

            if assignees:
                key = f"task_assigned_{tid}"
                if store.sensor_seen_state("plane_task_assigned", key) is None:
                    payload = {"task_id": tid, "title": title, "url": url}
                    self.publish(PLANE_TASK_ASSIGNED, payload)
                    store.mark_sensor_seen("plane_task_assigned", key, "seen")
                    emitted.append(payload)

            if due:
                due_dt = None
                try:
                    due_dt = _dt.datetime.fromisoformat(str(due))
                    if due_dt.tzinfo is None:
                        due_dt = due_dt.replace(tzinfo=_dt.timezone.utc)
                except Exception:
                    due_dt = None
                if due_dt is not None and due_dt <= cutoff:
                    key = f"task_due_{tid}_{due}"
                    if store.sensor_seen_state("plane_task_due", key) is None:
                        payload = {"task_id": tid, "title": title, "url": url, "due_date": str(due)}
                        self.publish(PLANE_TASK_DUE_SOON, payload)
                        store.mark_sensor_seen("plane_task_due", key, "seen")
                        emitted.append(payload)

            if state:
                status_key = f"task_status_{tid}"
                last_state = store.sensor_seen_state("plane_task_status", status_key)
                if last_state is not None and state != last_state:
                    payload = {"task_id": tid, "title": title, "url": url, "state": state}
                    self.publish(PLANE_TASK_STATUS_CHANGED, payload)
                    emitted.append(payload)
                if state != last_state:
                    store.mark_sensor_seen("plane_task_status", status_key, state)

        return emitted
