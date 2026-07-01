# PersonalOS — SaaS (multi-tenant) overview

The single-user personal app (`amy.app:app`) is unchanged. SaaS mode is a separate
app (`amy.saas.app:app`) that reuses the same core (vault, index, engine, agents,
captures) with a per-user tenancy layer. **No billing / no quotas** — every
authenticated user has full access.

## Phases built

| Phase | What | Docs |
|---|---|---|
| 1 | Accounts + JWT, per-user vaults, tenant isolation | `SAAS_PHASE1.md` |
| 2 | Background vault import (zip), vault management, deletion | `SAAS_PHASE2.md` |
| 3 | Bring-your-own OpenAI key, per-user captures | `SAAS_PHASE3.md` |
| 4 | Per-user privacy (private folders → local-only), account deletion | `SAAS_PHASE4.md`, `PRIVACY.md` |
| 5 | Mobile login + account UI (Flutter) | in `flutter_app/lib/` |
| 6 | Containerization + ops runbook | `OPERATIONS.md` |
| — | Billing | deliberately deferred |

## Architecture

```
Flutter app / web ──HTTPS + JWT──► amy.saas.app (FastAPI)
                                       ├─ accounts/auth ............ amy/saas/db.py, security.py
                                       ├─ per-user engine cache .... amy/saas/tenancy.py
                                       ├─ per-user vault + index ... saas_data/vaults|index/<uid>
                                       ├─ background import ........ amy/saas/imports.py
                                       └─ reuses core .............. amy/{engine,index,vault,captures,dynamic,agents}
```

Each user gets their own vault folder, vector collection (`vault_<uid>`), and Engine
— so data physically cannot cross tenants (proven by tests).

## Run it

```bash
pip install -r requirements.txt -r requirements-saas.txt
pytest tests/ -v          # all phases; tenant-isolation tests MUST pass
set AMY_JWT_SECRET=dev-secret
uvicorn amy.saas.app:app --host 0.0.0.0 --port 8849
```

Mobile: open the app → it shows the login screen → sign up / log in → set your
OpenAI key in Account → import a vault (web) or capture photos → chat with Amy.

## Tests (offline, no API key needed)

- `tests/test_tenant_isolation.py` — A can't see/retrieve B's notes.
- `tests/test_vault_import.py` — zip import loads notes, stays isolated, replace works.
- `tests/test_byok.py` — no shared key; user key enables OpenAI; sensitive → local only.
- `tests/test_captures_saas.py` — captures land in the right vault; dedup works.
- `tests/test_private_folders.py` — private folders mark notes sensitive.

## What to harden before real launch

- Argon2/bcrypt passwords (currently PBKDF2).
- Alembic migrations (init_db only creates missing tables).
- Managed Postgres + encrypted data volume; HTTPS.
- Move import worker to a task queue at scale; consider pgvector for the index.
- Then add billing as middleware on the existing endpoints.

I built all of this without being able to run it here, so **run `pytest tests/ -v`
first** — green tests confirm the multi-tenant foundation end to end.
