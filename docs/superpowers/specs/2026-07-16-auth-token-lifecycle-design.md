# Auth Token Lifecycle Hardening — Design Spec

**Date:** 2026-07-16
**Depends on:** Phase 0 foundation (auth, `users`/`company_users`, audit log) — merged to `main`. No dependency on any Phase 1–4 module.
**Scope source:** [`docs/07-security-compliance.md`](../../07-security-compliance.md) Section 1; gap identified in [`docs/requirements-vs-implementation-comparison-2026-07-12.md`](../../requirements-vs-implementation-comparison-2026-07-12.md) Sections 4.1–4.2 and named there as the "decide before this handles real subscriber data" item.

## Autonomy note

This spec was designed and executed under the user's explicit standing instruction ("upon completion of the merge, i want you to continue with the next task autonomously"), so the design decisions below were made by Claude using the project's own documents as the authority, not confirmed interactively. Each decision lists its rationale; any of them can be revisited before merge.

## Why now

docs/07 Section 1 has required, since v1.0: short-lived (~15 min) JWTs, a refresh-token rotation flow, and server-side refresh-token revocation "on logout, password change, or suspected compromise." None of that exists: access tokens live 60 minutes, there is no refresh token, no logout, no password-change route, and nothing server-side can invalidate a session before its token expires. The comparison doc flagged this as the top pre-subscriber-data decision. With Stripe billing, invoicing, and integrations all merged, the platform now handles exactly the data class that makes this urgent.

What is **already compliant** (verified directly, correcting the comparison doc's Section 4.1 overstatement): passwords are Argon2id (`argon2-cffi`, `app/core/security.py`), and login is timing-equalized against email enumeration.

## Decisions (made autonomously, rationale recorded)

1. **Harden the existing first-party HS256 JWT auth in place; do NOT adopt an external OIDC provider (Keycloak/Better Auth).** docs/07 says "OIDC/JWT-based" — the implementation has always been the JWT half, and every consumer (frontend scaffold, E2E script, 743 tests) assumes it. An external IdP is an infrastructure decision the user should make explicitly; rotation + revocation deliver the actual security property the section is after. Documented as a deliberate, revisitable deviation.
2. **Access tokens stay stateless; refresh tokens carry all revocability.** Access-token lifetime drops 60 → **15 minutes** (config default + `.env`/`.env.example`), which caps the exposure window of a stolen access token. The unused `jti` claim stays in the payload (no blocklist in v1 — a 15-minute stateless window is the accepted trade; noted in Deferred).
3. **Refresh tokens are opaque 256-bit secrets, not JWTs**, stored **hashed** (SHA-256 hex) in a new `refresh_tokens` table. The server never stores the presentable secret; a DB leak yields nothing replayable. Format: `secrets.token_urlsafe(32)`.
4. **Rotation with family-level reuse detection** (the standard OAuth-BCP design): every login creates a new *family* (`family_id`); every `/auth/refresh` marks the presented token revoked + `replaced_by_id` and issues a new token in the same family. Presenting a token that is already rotated or revoked is treated as **suspected compromise**: the entire family is revoked and the request gets 401. This is what makes revocation meaningful against a stolen refresh token that the attacker uses *after* the legitimate client has rotated past it — and it satisfies docs/07's "suspected compromise" revocation trigger mechanically rather than manually.
5. **`POST /auth/logout` takes the refresh token in the body and revokes its whole family; no bearer token required.** Possession of the refresh token is the credential (same reasoning as the OAuth callback and invitation-accept routes: the flow can't or needn't carry a bearer). Revoking the family (not just the one token) means logout means logout, even if an older rotation sibling leaked. Unknown/already-revoked token → still 204 (idempotent; a logout endpoint must not be an oracle for token validity).
6. **`POST /auth/change-password` (authenticated, any role) verifies the current password, re-hashes, and revokes ALL of the user's refresh tokens across every family/device**, per docs/07's "password change" trigger. Requires `current_password` so a hijacked 15-minute access token alone can't rotate the password. Writes an audit row. Returns 204. New password: same `min_length=8` rule as registration.
7. **`refresh_tokens` is user-scoped with NO RLS, like `users` itself.** A refresh token belongs to a person, not a tenant (one user can hold memberships in several companies). The table is never readable through any API; all queries filter on exact `token_hash` or `user_id` server-side. `app_user` gets SELECT/INSERT/UPDATE, **no DELETE** — revocation is an UPDATE (`revoked_at`), preserving the row as evidence, consistent with the audit-trail posture.
8. **Refresh lifetime 14 days** (`refresh_token_expire_days: int = 14` in Settings), absolute — v1 has no sliding idle window; each rotation issues a token with a fresh 14-day expiry, so an *active* session effectively continues, but any single token dies 14 days after issuance.
9. **`/auth/refresh` re-derives `default_company_id` with the exact membership query login uses** (ordered `created_at, company_id`), rather than freezing it into the refresh token row — membership changes (invitation accepted, membership removed) take effect at the next refresh. A user whose memberships are all gone refreshes into the same 403 login gives.
10. **Audit events**: `auth.password_changed` and `auth.refresh_reuse_detected` (the security-significant ones) write audit rows via the existing `write_audit_log`, scoped to the user's default company (the same membership the token flow resolves). Plain logins/refreshes/logouts are NOT audited (login never was; auditing every 15-minute refresh would be volume noise with no docs/07 mandate).

## Data model (migration 0014)

```sql
CREATE TABLE refresh_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    token_hash VARCHAR(64) NOT NULL UNIQUE,   -- SHA-256 hex of the opaque secret
    family_id UUID NOT NULL,                  -- rotation family, minted at login
    issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,                   -- NULL = live
    replaced_by_id UUID REFERENCES refresh_tokens(id),  -- set on rotation
    CHECK (replaced_by_id IS NULL OR revoked_at IS NOT NULL)  -- a rotated token can never still be redeemable
);
CREATE INDEX idx_refresh_tokens_user_id ON refresh_tokens (user_id);
CREATE INDEX idx_refresh_tokens_family_id ON refresh_tokens (family_id);
-- GRANT SELECT, INSERT, UPDATE ON refresh_tokens TO app_user;  (no DELETE)
-- No RLS (user-scoped, never API-readable; see Decision 7).
```

SQLAlchemy model `RefreshToken` in `app/models/refresh_token.py`, exported from `app/models/__init__.py`.

## Token service (`app/services/refresh_tokens.py`)

One module owns every lifecycle operation; routes stay thin:

- `mint_refresh_token(session, user_id, family_id=None) -> str` — generates the secret, inserts the hashed row (new family when `family_id` is None), returns the presentable secret. The ONLY place the plaintext secret exists.
- `_hash(secret) -> str` — `hashlib.sha256(secret.encode()).hexdigest()`.
- `rotate_refresh_token(session, presented_secret) -> tuple[RefreshToken, str]` — looks up by hash; raises `RefreshTokenError` on unknown/expired; on a row that is already revoked or rotated, revokes the whole family (`UPDATE ... WHERE family_id = ... AND revoked_at IS NULL`), emits the reuse audit row, and raises; otherwise marks it revoked+replaced and mints the successor in the same family.
- `revoke_family(session, family_id) -> None`
- `revoke_all_for_user(session, user_id) -> None`

All raises map to `401 "Invalid refresh token"` at the route — one message for unknown, expired, revoked, and reuse-detected alike (no oracle distinguishing them).

## API changes

| Route | Change |
|---|---|
| `POST /auth/login` | `TokenResponse` gains `refresh_token: str`. Login mints a new family. |
| `POST /auth/refresh` (new) | Body `{refresh_token}`. Rotates; returns a full `TokenResponse` (new access token, new refresh token, re-derived `default_company_id`). 401 on any invalid token. |
| `POST /auth/logout` (new) | Body `{refresh_token}`. Revokes the family. Always 204. |
| `POST /auth/change-password` (new) | Authenticated (`get_current_user`, no role gate — self-service). Body `{current_password, new_password}`. 401 wrong current password; on success re-hash + `revoke_all_for_user` + audit row; 204. |

None of the three new routes takes `block_if_read_only` or `require_module` — session management must work for read-only (canceled) companies too, exactly as `/auth/login` already does, and it is not a tiered module.

## Config

- `jwt_expire_minutes: int = 60` → `15` (default), `.env` and `.env.example` updated to 15. `backend/tests/conftest.py` keeps its explicit test value (60) — token lifetime is irrelevant to test flow, and pinning it avoids ever chasing phantom expiries in a slow debug session.
- New `refresh_token_expire_days: int = 14`.

## Error handling

- Unknown / expired / revoked / reused refresh token: uniform `401 "Invalid refresh token"`.
- Reuse detection additionally revokes the family and writes `auth.refresh_reuse_detected` before the 401 leaves.
- `change-password` with a wrong `current_password`: `401 "Invalid current password"` (does NOT revoke anything — a wrong guess by a token-holding attacker must not DoS the real user's sessions; the audit row on *success* plus 401 noise is the signal).
- Logout is deliberately non-erroring (204 even for garbage input).

## Testing strategy

New `backend/tests/test_auth_token_lifecycle.py`:

1. Login returns both tokens; refresh returns a *different* access token and *different* refresh token; old access token still decodes (stateless) but old refresh is dead.
2. Rotation chain: login → refresh → refresh; each presented token works exactly once.
3. **Reuse detection**: login → refresh (get B from A) → present A again → 401 AND B (the legitimate successor) is now also 401 — the family died. Audit row `auth.refresh_reuse_detected` exists (owner-DSN check).
4. Expired refresh token → 401 (row's `expires_at` back-dated via owner DSN, same direct-DB test-setup precedent as `set_subscription_tier`).
5. Logout → 204; the token no longer refreshes; second logout with the same token still 204.
6. Change-password: wrong current → 401 and refresh still works; correct current → 204, ALL prior refresh tokens dead (both families from two logins), old password no longer logs in, new one does, audit row exists.
7. Refresh re-derives membership: the response's `default_company_id` matches login's.
8. Token-hash hygiene: the stored `token_hash` is 64 hex chars and is NOT the presented secret (owner-DSN check).
9. Config: access-token `exp - iat` honors `settings.jwt_expire_minutes` (guards the 15-minute default without pinning tests to wall-clock).

Plus: the existing full suite must stay green (every existing test logs in; `TokenResponse` gains a field, which is additive for every consumer).

E2E (`scripts/e2e_smoke_test.py`): one new block or an extension of Company A's — login → refresh → old-refresh reuse → 401 → logout of the new family → 401, proving rotation/revocation live over real HTTP.

## Deferred (explicitly out of scope)

- **MFA/TOTP for the Admin role** — docs/07 requires it "at minimum" for Admins; it is an independent subsystem (enrollment, activation, login challenge, recovery) and is the **next spec after this one**, not silently dropped.
- External OIDC provider (Keycloak/Better Auth) — Decision 1.
- Access-token `jti` blocklist (revoking access tokens mid-window).
- Refresh-token row pruning/retention job (expired+revoked rows are inert; add a scheduler task if volume ever matters).
- Frontend session handling (no app UI exists yet).
- `docs/05-api-specification.md` gains the three new routes in its table (same doc-sync convention as tier gating's docs/08 update).
