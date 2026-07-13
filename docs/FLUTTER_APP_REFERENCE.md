# Flutter app reference — `personal-os-app`

Reference doc for `guru-prasath-j/personal-os-app` (GitHub), fetched and
verified live against both that repo and this one (`saas-os`) on
2026-07-14. Written so another Claude session can pick up enhancement
work with real facts instead of re-discovering them.

**Use alongside `docs/BACKEND_NOT_IN_UI.md`** — that doc lists saas-os
backend features with no UI anywhere (web or mobile); this doc is about
the mobile client itself and where it currently disagrees with saas-os's
actual API.

## What it is

A small (9 Dart files, `lib/`) Flutter client for "the Amy/Jarvis
backend" — voice/chat + photo capture. Repo README title: "Amy — Flutter
Client (PersonalOS)". Two jobs, per its own README:

1. **Chat/voice with Amy** — talks to `/ws` + `/api/query`.
2. **Photo capture** — take/pick a photo; backend captions + OCRs it,
   records GPS/timestamp, writes a vault note under `08_Captures/`.

## Source layout (`lib/`)

| File | Role |
|---|---|
| `main.dart` | App entry + `HomePage` — chat UI, STT (`speech_to_text`), TTS (`flutter_tts`), wires `AmyApi`/`AmySocket`. |
| `config.dart` | `Config` — persisted `baseUrl` (default `http://10.0.2.2:8848`, the Android-emulator-to-host address) + `token`, via `shared_preferences`. Builds `wsUrl` and auth headers. |
| `api.dart` | `AmyApi` — HTTP client: `stats()`, `query(text, channel)`, `uploadCapture(...)` (multipart). |
| `ws.dart` | `AmySocket` — WebSocket client for `/ws`; sends `{"token": ...}` as the **first message** on connect (not a header) when a token is set. |
| `capture_screen.dart` | In-app camera/gallery capture flow (capture mode B). |
| `captures_screen.dart` | Browse past captures. |
| `gallery_sync.dart` | Background gallery auto-watch/sync (capture mode A, Android, via `photo_manager`). |
| `share_handler.dart` | Receives share-to-Amy intents from other apps (capture mode C, via `receive_sharing_intent`). |
| `settings_screen.dart` | Edit server URL + token (writes to `Config`). |

Dependencies of note (`pubspec.yaml`): `http`, `web_socket_channel`,
`speech_to_text`, `flutter_tts`, `image_picker`, `geolocator`,
`shared_preferences`, `receive_sharing_intent`, `photo_manager`.

## Backend it was actually built against — NOT this repo

The README says `python main.py --mode personal --host 0.0.0.0 --port
8848` from a `_Amy/` directory. **That is a different, single-user,
non-SaaS Amy backend** (`--mode personal` flag, a plain `main.py`, port
**8848**) — not `saas-os`, which is a multi-tenant FastAPI app run via
`python -m uvicorn amy.saas.app:app --port 8849` (see this repo's
`README.md`/`CLAUDE.md`). They are sibling/ancestor projects, not the
same server. **Do not assume the app works against saas-os without
checking each endpoint** — three do, one doesn't, auth doesn't:

| App expects | saas-os reality | Verdict |
|---|---|---|
| `POST /api/query`, `GET /api/stats` | Both exist verbatim in `amy/saas/routers/vault.py`, gated by `Depends(current_user)` (JWT). | **Compatible**, once auth is fixed (below). |
| `POST /api/captures` (multipart: `file`, `lat`, `lon`, `taken_at`, `source`, `note`, `tags`) | Exists verbatim in `amy/saas/routers/captures.py` with the exact same field names, same `Depends(current_user)`. | **Compatible**, once auth is fixed. |
| `GET /api/captures?limit=`, `GET /api/captures/image?path=` | Both exist in the same router. | **Compatible.** |
| `WS /ws`, first-message `{"token": ...}` auth | **Does not exist.** Grepped `amy/saas/app.py` and every router — no `@app.websocket` or `.websocket(` route anywhere in saas-os. | **Missing entirely.** Chat/voice over the socket will fail to connect; `main.dart`'s `sock.connect()` path needs a fallback or saas-os needs a `/ws` route added. |
| Settings screen: a bare "token" field, sent as `Authorization: Bearer <token>` (`Config.authHeaders`) or as the WS first message | saas-os has **no static token** — auth is `POST /auth/signup` / `POST /auth/login` (email+password) returning a JWT (`amy/saas/routers/auth.py`), same JWT then required as `Authorization: Bearer` on every route. The legacy backend's README hints at a static `AMY_AUTH_TOKEN` env var model instead. | **Mismatch.** The token field needs to become an actual login screen (call `/auth/login`, persist the returned JWT) instead of a free-text token paste. |

## What "enhance this app to work with saas-os" actually requires

1. **Add a login flow.** `settings_screen.dart` currently just persists a
   free-typed token. Needs an email/password form calling
   `POST /auth/login` (`{"email", "password"}` → `{"token": ..., "user":
   {...}}`, mirroring `tests/test_career_routes.py`'s `app_client` fixture
   for the exact contract) and storing the returned JWT in `Config.token`
   — the existing `Config.authHeaders()` plumbing then works unchanged for
   `/api/query`, `/api/stats`, `/api/captures`.
2. **`/ws` doesn't exist on saas-os yet.** Two options, not yet decided
   by anyone: (a) add a websocket route to saas-os (bigger, touches the
   multi-tenant session model — every other route resolves the user via
   `Depends(current_user)` per-request; a websocket needs the JWT
   validated once at connect instead), or (b) change `main.dart`'s chat
   path to poll `POST /api/query` over plain HTTP and drop `ws.dart`
   entirely for saas-os specifically (smaller, no backend change, loses
   streaming/push behavior if `/api/query` doesn't stream — check its
   response shape in `amy/saas/routers/vault.py` before assuming parity).
3. **Default `baseUrl`** (`http://10.0.2.2:8848`) needs to become
   `:8849` for saas-os, and the setup docs' `--mode personal` instructions
   are for the wrong backend entirely — replace with this repo's actual
   run command (`README.md`'s Quick start).
4. **`/api/captures` behavior differs slightly from the app's
   expectations** — worth re-checking `captures_mod.ingest()`'s
   captioning/OCR path (`amy/captures.py`) against what the README
   promises, since saas-os's captures pipeline has evolved independently
   of this app (multi-tenant, `_user_key(user)`-scoped OpenAI key,
   per-user vault resolution) since the app's README was written.

## Not yet checked (flag before relying on it)

- Whether `POST /api/query`'s response shape (`_dump(eng.ask(...))`,
  `ContextModule`/`ask()` in `amy/context.py` or wherever `_engine_for`
  resolves to) matches what `main.dart`'s `HomePage` expects to render
  (it parses `Msg` objects with `who`/`text`/`meta`/`confirmId` — the
  `confirmId` implies an approval-confirmation UX that may or may not
  have a saas-os equivalent on this route specifically, as opposed to the
  separate Approval Inbox this repo actually uses for agent writes).
- Android manifest / iOS Info.plist permission patching
  (`setup_mobile.ps1`) — not re-verified against a current Flutter SDK;
  README calls out that `flutter create .` overwrites some of this.
- No test suite in the Flutter repo (`test/` exists in the tree but
  wasn't inspected) — unknown coverage.
