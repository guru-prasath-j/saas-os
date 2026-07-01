# SaaS — Phase 1 (Accounts + Per-User Vaults + Tenant Isolation)

This implements Phase 1 of `new_prompt.md`: user accounts, JWT auth, a per-user
data model, and a **cross-tenant isolation test**. Your single-user personal app
(`amy.app:app`) is unchanged — this is a separate app (`amy.saas.app:app`).

## What's included

| File | Role |
|---|---|
| `amy/saas/db.py` | SQLAlchemy models (`User`) + session. SQLite by default; Postgres via `DATABASE_URL`. |
| `amy/saas/paths.py` | Per-user vault / index / uploads folders + collection name. |
| `amy/saas/security.py` | PBKDF2 password hashing, JWT tokens, Fernet API-key encryption. |
| `amy/saas/tenancy.py` | One `Engine` per user (own vault folder + own vector collection), cached. |
| `amy/saas/app.py` | FastAPI: signup / login / me / set-key / vault import / query / stats. |
| `tests/test_tenant_isolation.py` | Proves user A can never see/retrieve user B's notes. |
| `amy/engine.py`, `amy/index.py` | Parameterized to accept per-user vault/index (backward compatible). |

## Install

```bash
pip install -r requirements.txt
pip install -r requirements-saas.txt
```

## Run the cross-tenant isolation test FIRST

```bash
cd _Amy
pytest tests/test_tenant_isolation.py -v
```

It creates two users, seeds each with a private note, and asserts each engine
only ever loads and retrieves its own notes (even when searching for the other
user's exact codeword). This is the core guarantee — keep it green.

## Run the SaaS API

```bash
# AMY_DYNAMIC_AGENTS=1 is required so each user's own folders become their agents
# Windows:
set AMY_DYNAMIC_AGENTS=1
set AMY_JWT_SECRET=change-me-to-a-long-random-string
uvicorn amy.saas.app:app --host 0.0.0.0 --port 8849
```

```bash
# macOS/Linux:
AMY_DYNAMIC_AGENTS=1 AMY_JWT_SECRET=change-me uvicorn amy.saas.app:app --port 8849
```

## Try the flow with curl

```bash
# 1. sign up -> get a token
curl -s -X POST localhost:8849/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email":"a@test.com","password":"password123"}'
# -> {"token":"<JWT>", ...}

TOKEN=<paste JWT>

# 2. import a vault (zip of an Obsidian folder)
curl -s -X POST localhost:8849/api/vault/import \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@my_vault.zip"

# 3. ask Amy (scoped to YOUR vault only)
curl -s -X POST localhost:8849/api/query \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"what notes do I have about work?"}'
```

A second user who signs up and imports their own zip will get completely separate
data — verified by the isolation test.

## Environment variables

| Var | Purpose | Prod note |
|---|---|---|
| `DATABASE_URL` | DB connection | set to Postgres in prod |
| `AMY_JWT_SECRET` | signs auth tokens | **must** be a long random secret |
| `AMY_ENC_SECRET` | encrypts stored OpenAI keys | set independently in prod |
| `AMY_SAAS_DATA` | where per-user vaults/indexes live | a persistent disk/volume |
| `AMY_DYNAMIC_AGENTS=1` | per-user folder agents | required for SaaS |

## What Phase 1 does NOT yet do (next phases)

- **Phase 3 — BYO key wiring:** the user's OpenAI key is stored encrypted, but the
  LLM router still uses the global key. Wire `decrypt_secret(user.openai_key_enc)`
  into a per-user `LLMRouter` so each user's calls use their own key.
- **Phase 3 — captures per user:** `/api/captures` is still single-user; move it
  under the tenant layer (write into the user's vault dir).
- **Phase 4 — privacy:** encryption at rest for vault contents, account deletion.
- **Phase 5 — mobile/web login UI.**  **Phase 6 — billing + ops.**

See `new_prompt.md` for the full phase plan.
