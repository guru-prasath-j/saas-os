# PersonalOS SaaS — Operations Runbook

How to deploy and run the multi-tenant backend. **Billing is intentionally not
included** — every authenticated user has full access; add billing later as
middleware without changing these endpoints.

## Required environment variables (production)

| Var | Purpose | Notes |
|---|---|---|
| `AMY_JWT_SECRET` | signs login tokens | long random string; rotating it logs everyone out |
| `AMY_ENC_SECRET` | encrypts stored OpenAI keys | **different** from JWT secret; if lost, stored keys can't be decrypted |
| `DATABASE_URL` | DB connection | `postgresql+psycopg://user:pass@host/db` in prod; defaults to SQLite |
| `AMY_SAAS_DATA` | per-user vaults + indexes | a **persistent, encrypted** volume |
| `AMY_DYNAMIC_AGENTS` | per-user folder agents | set `1` (the SaaS app sets this by default) |

Optional: `AMY_JWT_TTL_HOURS` (default 168), `AMY_MAX_CACHED_ENGINES` (default 200).

## Run locally

```bash
pip install -r requirements.txt -r requirements-saas.txt
pytest tests/ -v                       # all phases — keep green
set AMY_JWT_SECRET=dev-secret          # (export on macOS/Linux)
uvicorn amy.saas.app:app --host 0.0.0.0 --port 8849
```

## Run with Docker

```bash
docker compose -f docker-compose.saas.yml up --build
# data persists in the amy_data volume across restarts
```

## Deploy options

**Fly.io / Render / Railway (container):**
1. Push the repo. Point the service at `Dockerfile.saas`.
2. Add a **persistent volume** mounted at `/data` (this is `AMY_SAAS_DATA`).
3. Set secrets: `AMY_JWT_SECRET`, `AMY_ENC_SECRET`, and `DATABASE_URL`
   (provision managed Postgres). Expose port `8849` behind the platform's HTTPS.

**VPS:**
1. Install Docker, clone repo, create `.env.saas` with the secrets.
2. `docker compose -f docker-compose.saas.yml up -d --build`.
3. Put **Caddy/Nginx** in front for HTTPS (or a Cloudflare Tunnel).

## Database

- Dev: SQLite file at `$AMY_SAAS_DATA/amy_saas.db` (zero setup).
- Prod: set `DATABASE_URL` to Postgres and `pip install "psycopg[binary]"`.
- **Schema migrations:** `init_db()` only *creates* missing tables; it does not
  ALTER existing ones. Adding columns later (we added `sensitive_folders`) needs a
  migration tool (Alembic) on an existing prod DB.

## Backups

- **`$AMY_SAAS_DATA`** holds every user's vault + vector index → back it up
  (volume snapshots or `restic`/`rclone` to object storage on a schedule).
- **Database** → managed Postgres automated backups, or `pg_dump` cron.

## Security checklist before launch

- [ ] `AMY_JWT_SECRET` and `AMY_ENC_SECRET` set to strong, distinct secrets.
- [ ] HTTPS enforced (platform TLS, Caddy/Nginx, or Cloudflare).
- [ ] `AMY_SAAS_DATA` volume + DB on **encrypted** storage with locked-down access.
- [ ] Swap PBKDF2 → Argon2/bcrypt for passwords (see `amy/saas/security.py`).
- [ ] Run the test suite; the **tenant isolation** tests must pass.
- [ ] `PRIVACY.md` reviewed; a public privacy policy published.

## Scaling notes

- The import worker runs in a background **thread** (`amy/saas/imports.py`). For
  many concurrent large imports, move to a task queue (Celery/RQ/Arq) — same worker
  function, different dispatch.
- Per-user engines are cached in memory (`AMY_MAX_CACHED_ENGINES`, FIFO eviction).
  For multiple app instances, ensure sticky sessions or rely on the lazy rebuild
  (each instance reloads a user's engine from the shared `AMY_SAAS_DATA` volume).
- The vector store is per-user Chroma collections on the data volume. At large
  scale, migrate to a managed vector DB (e.g. pgvector in the same Postgres).

## Endpoints (SaaS)

```
POST /auth/signup, /auth/login
GET  /api/me
POST /api/settings/openai-key      DELETE /api/settings/openai-key
GET/PUT /api/settings/private-folders
POST /api/vault/import   GET /api/vault/import/{job_id}
GET  /api/vault          GET /api/notes          DELETE /api/vault
POST /api/query          GET /api/stats
POST /api/captures       GET /api/captures       GET /api/captures/image
DELETE /api/account
GET  /api/health
```
