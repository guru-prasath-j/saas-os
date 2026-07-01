# SaaS — Phase 2 (Production Vault Import + Per-User Collections + Management)

Builds on Phase 1. **No billing / no quotas** — every signed-in user gets full
access. (Billing is intentionally deferred; add it in a later phase.)

## What's new in Phase 2

| Capability | Endpoint / file |
|---|---|
| **Async vault import** (background job, doesn't block the request) | `POST /api/vault/import` → `{job_id}` |
| **Import status** (poll until done) | `GET /api/vault/import/{job_id}` |
| **Index warming** (build the user's vectors at import, not lazily) | `amy/saas/imports.py` → `tenancy.warm()` |
| **Vault info** (note count + the agents discovered from their folders) | `GET /api/vault` |
| **Notes list** (paginated) | `GET /api/notes?limit=&offset=` |
| **Delete vault** (wipe vault + index + vector collection) | `DELETE /api/vault` |
| **Delete account** (all data + the account, GDPR-style) | `DELETE /api/account` |
| Per-user Chroma collection teardown | `amy/index.py` → `drop_index()` |

The Phase 1 synchronous import was replaced by the job-based flow.

## Import flow

```
POST /api/vault/import (zip)         -> validates zip, saves it, creates ImportJob(pending), returns {job_id}
   worker thread (amy/saas/imports)  -> running -> extract (zip-slip guarded) -> reload vault -> build index
GET  /api/vault/import/{job_id}      -> {status: done, markdown_notes, notes_loaded}
```

> Production note: the worker runs in a background **thread** (fine for now). At
> scale, swap `imports.start()` for a real task queue (Celery/RQ/Arq) — the worker
> function `run_import()` stays identical.

## Run the tests

```bash
cd _Amy
pip install -r requirements-saas.txt
pytest tests/test_tenant_isolation.py tests/test_vault_import.py -v
```

`test_vault_import.py` covers: notes load from a zip, **tenants stay isolated after
import**, and re-import replaces the old vault.

## Try it

```bash
set AMY_DYNAMIC_AGENTS=1
set AMY_JWT_SECRET=change-me
uvicorn amy.saas.app:app --host 0.0.0.0 --port 8849

# signup -> token
curl -s -X POST localhost:8849/auth/signup -H "Content-Type: application/json" \
  -d '{"email":"a@test.com","password":"password123"}'
TOKEN=<jwt>

# import (returns a job id)
curl -s -X POST localhost:8849/api/vault/import \
  -H "Authorization: Bearer $TOKEN" -F "file=@my_vault.zip"
# poll
curl -s localhost:8849/api/vault/import/<job_id> -H "Authorization: Bearer $TOKEN"
# see the agents auto-built from your folders
curl -s localhost:8849/api/vault -H "Authorization: Bearer $TOKEN"
```

## Access model (current)

No plans, no limits — any authenticated user can import vaults, query, and use
all agents. When you're ready, billing/quotas slot in as middleware on these
same endpoints without changing their logic.

## Next phases

- **Phase 3** — wire each user's stored OpenAI key into a per-user LLM router;
  move `/api/captures` under the tenant layer (write into the user's vault).
- **Phase 4** — encryption at rest for vault contents; per-note sensitivity controls.
- **Phase 5** — mobile/web login + import UI.
- **Phase 6** — billing + ops/deploy.
