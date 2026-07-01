# PersonalOS SaaS — Privacy Model

How user data is isolated, protected, and kept private. Pair this with a public
privacy policy before launch.

## Data each user has
- **Vault** (their uploaded markdown notes) — on disk under `saas_data/vaults/<user_id>/`.
- **Vector index** — per-user collection `vault_<user_id>`.
- **Captures** — photos + capture notes inside their own vault (`08_Captures/`).
- **OpenAI key** — stored **encrypted** (Fernet) in the DB; never logged or returned.
- **Account row** — email + hashed password (PBKDF2; use Argon2/bcrypt for launch).

## Isolation (multi-tenant)
- Every `/api` route is scoped to the authenticated user (JWT → `user_id`).
- Each user gets a **separate vault folder, vector collection, and Engine**, so
  retrieval physically cannot cross tenants.
- Enforced by tests: `tests/test_tenant_isolation.py`, `tests/test_vault_import.py`.

## Sensitive data handling
- **Bring-your-own-key:** a user's content is only ever sent to **their own** OpenAI
  key. A shared/platform key is never used for user content (`use_global_keys=False`).
- **Local-only for sensitive notes:** any note tagged `sensitive`, or any note in a
  folder the user marked **private** (`/api/settings/private-folders`), routes to the
  **local model only** (Ollama) and is never sent to a cloud key. Proven by
  `tests/test_byok.py` and `tests/test_private_folders.py`.

## Deletion (GDPR-style)
- `DELETE /api/vault` wipes a user's vault, index dir, and vector collection.
- `DELETE /api/account` removes all of the above **and** the account + job rows.

## Encryption at rest (deployment responsibility)
- OpenAI keys are app-encrypted (Fernet, key from `AMY_ENC_SECRET`).
- **Vault files and the DB are not app-encrypted** (they must stay readable for
  indexing/serving). Protect them with **disk/volume encryption** on the host
  (e.g. encrypted EBS/volume, or full-disk encryption) and locked-down access.
- For a stronger tier, offer a **self-host / on-device** option so sensitive vaults
  never leave the user's machine (the personal app already does this).

## Secrets / config (must be set in production)
| Var | Purpose |
|---|---|
| `AMY_JWT_SECRET` | signs auth tokens (long random) |
| `AMY_ENC_SECRET` | encrypts stored OpenAI keys (separate from JWT secret) |
| `DATABASE_URL` | Postgres in prod |
| `AMY_SAAS_DATA` | persistent, access-controlled, encrypted volume |

## Not yet implemented (future hardening)
- Argon2/bcrypt password hashing (currently PBKDF2-HMAC-SHA256).
- Per-note sharing / access controls.
- Audit log of every AI access.
- End-to-end encrypted vaults (premium tier).
