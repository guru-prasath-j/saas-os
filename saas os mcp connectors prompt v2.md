# Prompt: Add MCP-based connectors to saas-os (two-layer design, phased rollout)

Paste this into Claude Code (or a fresh chat) inside the `saas-os` repo.

---

## Context (read first, don't skip)

`saas-os` (package `amy/`) currently has hand-rolled, per-service API clients for
external data — e.g. `amy/saas/routers/connectors.py` (Google OAuth), the
Gmail 3-pass sync in `amy/finance/`, and a mostly-stub `amy/sensors/github_sensor.py`.
Each new source today means writing a new bespoke client from scratch.

We are replacing this with a **generic MCP connector layer**, split into two
distinct pieces that must not be conflated:

1. **Layer 1 — Generic connector registration (dynamic, no-code).**
   A user can add any MCP-compatible source from the UI with just a name,
   server URL, and auth (API key or OAuth) — the same shape as CORE's
   `settings.mcp-integrations` form. This makes the connected server's tools
   callable by the AI agent immediately. It does **not** by itself write
   anything to the vault or event log.

2. **Layer 2 — "Promote to sensor" (explicit, small amount of per-source logic).**
   A separate, deliberate step that takes a Layer-1 connection and turns it
   into a polling/webhook loop that normalizes activity into events via
   `events.emit()`, the same shape as `amy/sensors/github_sensor.py` already
   does — which then flows through the existing `MemoryWriter` into
   `00_Daily/`, `01_Weekly/`, and `09_Memory/` vault notes.

Do not let Layer 1 quietly imply Layer 2. A source can be connected and
queryable without ever writing to the vault; only sources explicitly promoted
should generate events.

---

## Step 0 — Mandatory audit before writing any code

Before implementing anything:

1. Read the current state of every file this will touch:
   `amy/saas/routers/connectors.py`, `amy/finance/gmail_import.py` (or wherever
   the Gmail sync lives), `amy/sensors/github_sensor.py`, `amy/events/store.py`,
   `amy/memory/writer.py`, and the dashboard's existing Google connector card
   in the frontend (`index.html`, the `connectGoogle()` / `loadGoogleStatus()`
   functions and their card markup).
2. Map every current write path that touches external data, and note which
   ones already call `events.emit()` and which bypass it (Gmail import
   currently bypasses it — confirm whether that's still true).
3. Write out a short plan: what's reused as-is, what's refactored, what's net
   new, and which files change. Include the phased order below.
4. **Stop and show me the plan. Get my explicit sign-off before writing any
   code.** Do not proceed to implementation in the same turn as the audit.

---

## Architecture to build

### `MCPConnector` (generic client)
A single reusable class/module that, given a server URL + auth (API key
header or OAuth token), can:
- Connect via SSE or HTTP MCP transport
- `list_tools()`
- `call_tool(name, arguments)`
- Store credentials the same way `connectors.py` already stores Google's
  (per-user, encrypted at rest — reuse existing storage, don't invent a new
  credential table)

### Connector registry
A table/model with at least:
`id, user_id, name, server_url, auth_type, auth_ref, risk_tier, promoted_to_sensor (bool), created_at`

`risk_tier` is a required field — `official`, `platform_api`, `scraping_backed`,
`unofficial_risky` — set per source at registration time, not left blank.
Surface this tier in the UI card (a small label/badge) so the user always
sees which category a connected source falls into.

### UI changes
- Extend the existing "Account" section pattern (the Google card) with a
  generic "Add MCP source" card: name, server URL, auth fields — mirrors
  `settings.mcp-integrations` in CORE.
- Each connected source gets the same `Connect / Sync now / Disconnect`
  buttons already used for Google.
- A second, explicit action per card — "Also sync to vault" (Layer 2) —
  separate from the connect action. Only sources with this enabled register
  a sensor loop.
- Show `risk_tier` as a small badge on non-`official` sources.

### Sensor promotion (Layer 2)
Reuse the shape of `github_sensor.py`: normalize incoming activity into a
canonical event, `events.emit()` it, let the existing `MemoryWriter`
subscriber handle vault writes. Do not write directly to the vault from a
sensor — always go through the event bus, per the existing pattern.

---

## Build order (phased — build and verify each phase before starting the next)

### Phase 1 — Tier A: official first-party MCP servers (build first)
- **GitHub** — `api.githubcopilot.com/mcp/x/all`
- **Plane** — `mcp.plane.so` (project/issue/cycle tracking — career module)
- **KITE (Zerodha)** — `mcp.kite.trade` (holdings/positions/orders — finance
  module, "CFO mode")

These are `risk_tier: official`. No scraping, no ToS ambiguity. Wire all
three through `MCPConnector`, promote GitHub to a sensor first since
`github_sensor.py` already has the stub to build on.

### Phase 2 — Tier B: platform APIs needing real OAuth setup
- **Outlook** — via Microsoft Graph (e.g. `ms-365-mcp-server`), device-code
  auth for a personal account
- **Teams** — same Graph path; note that most Teams data requires
  `--org-mode` (work/school account) — a personal account will see limited
  data. Document this limitation in the UI rather than silently returning
  nothing.

`risk_tier: platform_api`. Requires an Azure app registration — flag this as
a manual setup step for the user, not something the connector form alone
solves.

### Phase 3 — Job search (Bayt/Naukri/Indeed/LinkedIn jobs)
- Use `jobspy-mcp-server` (covers `indeed, linkedin, zip_recruiter, glassdoor,
  google, bayt, naukri` — Bayt is the relevant Dubai/Gulf portal) or the
  LoopCV hosted MCP as an alternative.

`risk_tier: scraping_backed`. This must show clearly in the UI — a visible
label like "unofficial data source" on the connector card — so it's never
mistaken for an official feed. Do not silently treat job-search results with
the same trust level as Phase 1/2 sources in any downstream ranking or
citation logic.

### Phase 4 — Telegram (bot API only)
- Bot-API-based MCP servers only. Do **not** implement personal-account
  (MTProto) access — that risks account suspension per Telegram's own terms.

`risk_tier: official` (bot path) — but scope the implementation strictly to
bot-API tools; if a personal-account MCP server is ever added later, it must
default to `risk_tier: unofficial_risky` and require an explicit
confirmation step before first use.

### Explicitly out of scope for this pass
- **WhatsApp** (personal number) — no official API, real ban risk via
  reverse-engineered clients (flagged as a "lethal trifecta" prompt-injection
  risk pattern by the community server itself). Skip.
- **LinkedIn** (personal profile) — no official MCP server exists; all
  practical options are scraping-based and against ToS. Skip, or if added
  later, isolate under `unofficial_risky`, throwaway-account use only, and
  never the user's real profile.

If either is added later, they must go through the same `risk_tier` gate as
Phase 3/4 and get their own explicit sign-off — don't fold them into a future
phase silently.

---

## Acceptance criteria per phase

For each source added:
- [ ] Connects via `MCPConnector` from the UI with no code changes needed for
      future same-shape sources
- [ ] `risk_tier` set and visible in the UI
- [ ] Layer 1 (connect/query) and Layer 2 (sensor/vault sync) are separately
      toggleable
- [ ] If promoted to sensor: events flow through `events.emit()`, idempotent
      via the existing `eid` marker pattern, and land in `00_Daily/` correctly
- [ ] No source bypasses the event bus to write to the vault directly
- [ ] Existing Google connector and Gmail sync continue to work unchanged
      unless the audit in Step 0 found a specific reason to touch them

Stop after each phase and confirm before starting the next.
