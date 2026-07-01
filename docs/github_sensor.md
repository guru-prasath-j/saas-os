# GitHub Sensor

GitHub is integrated as a **Sensor**, not an agent. A Sensor authenticates to an
external system, normalizes incoming data into canonical events, and publishes
them to the Event Bus. Agents then subscribe and react. This keeps the
integration decoupled from any reasoning.

```
GitHub  ->  GitHub Sensor  ->  Event Bus  ->  Relevant Agents
```

## Security (non-negotiable)

**The GitHub token is never stored in code.** `GitHubService` reads it from the
`GITHUB_TOKEN` environment variable only. If no token is present the service
runs in **offline mode** and simply returns nothing — the rest of PIOS keeps
working. The webhook endpoint never accepts a token in its body; webhook
deliveries are authenticated by your reverse proxy / GitHub secret, and polling
uses the env token.

## Components

```
sensors/github_models.py   GitHubEvent + canonical event types
sensors/github_service.py  auth (env token) + raw fetch (stdlib urllib)
sensors/github_sensor.py   normalize raw payloads -> publish to Event Bus
```

## Events

Published on the bus as `github.<TYPE>`:

| Event              | Source trigger                         |
|--------------------|----------------------------------------|
| `NEW_REPOSITORY`   | repo created                           |
| `NEW_COMMIT`       | push                                   |
| `NEW_PULL_REQUEST` | pull request opened/updated            |
| `NEW_ISSUE`        | issue opened/updated                   |
| `NEW_RELEASE`      | release published                      |
| `CI_FAILURE`       | workflow/check run concluded `failure` |

Each becomes a normalized `GitHubEvent` (`repo`, `title`, `actor`, `url`, `ts`,
`extra`) so downstream code never depends on GitHub's raw payload shape.

## Ingestion paths

- **Webhooks** — `ingest_webhook(event_name, payload)` where `event_name` is the
  `X-GitHub-Event` header. Maps push / pull_request / issues / release /
  repository / workflow_run deliveries.
- **API feed** — `ingest_raw(raw_events)` / `poll(owner_repo)` normalize the
  `/repos/{owner}/{repo}/events` feed (polling needs a token).

A success/non-failure CI run is intentionally **not** published — only
`CI_FAILURE` fires, so agents aren't spammed with green builds.

## API

| Method & path                     | Purpose                              |
|-----------------------------------|--------------------------------------|
| `POST /api/sensors/github/webhook`| receive a webhook, publish events    |
| `POST /api/sensors/github/poll?repo=owner/name` | poll a repo feed (needs token) |

Published events land in the per-user `events` table and are dispatched to any
subscriber, exactly like internal events — so existing agents can react to
GitHub activity with no special-casing.

## Resilience

All network calls are best-effort: failures (no token, network error, bad JSON)
return empty results rather than raising, and a bad subscriber never breaks the
sensor.
