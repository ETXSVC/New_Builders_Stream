# MFA (TOTP) — Design Spec

**Date:** 2026-07-16
**Depends on:** Auth token lifecycle (PR #13, merged — `/auth/login` challenge point, `change-password` hardening point, audit conventions), Phase 0 auth, the Fernet encryption service (Task 4.3).
**Scope source:** [`docs/07-security-compliance.md`](../../07-security-compliance.md) Section 1: "Multi-factor authentication (TOTP) is a requirement for the Admin role at minimum, strongly recommended for all roles — should be scoped into an early phase given the platform handles client financial/contract data." Named as the explicit follow-up spec by the token-lifecycle spec's Deferred section.

## Autonomy note

Designed and executed under the user's standing autonomous-continue instruction; decisions below were made by Claude from the project's own documents and are all revisitable before merge. The PR at the end waits for explicit merge authorization (the prior "commit and merge" authorization was for the token-lifecycle PR specifically).

## Decisions (made autonomously, rationale recorded)

1. **Standard RFC 6238 TOTP via `pyotp`** (new dependency): 6 digits, 30-second period, SHA-1 — the parameters every mainstream authenticator app assumes. No SMS/email second factors (docs/07 says TOTP).
2. **Available to ALL roles, enforced at login once activated.** Enrollment is per-user self-service; docs/07's "strongly recommended for all roles" costs nothing extra once the Admin path exists.
3. **Admin "requirement" is a soft server-side signal in v1, not a hard login block.** `TokenResponse` gains `mfa_enrollment_required: bool` — true when the user's default membership role is `admin` and MFA is not activated. A hard block would deadlock every fresh registration (registration creates an admin who has never had a chance to enroll) unless we built an enrollment-limbo token flow, and it would invalidate ~750 existing tests' login helpers. The flag is the server-side truth the (future) frontend enforces as a forced-enrollment screen; revisiting to a hard block with a limbo-token flow is explicitly future work, recorded here so docs/07's "requirement" wording is honestly tracked rather than silently satisfied.
4. **Storage: two nullable columns on `users`** (migration 0015) — `totp_secret_encrypted TEXT` (Fernet, via the EXISTING `app/services/token_encryption.py`; deliberate key reuse: same threat class — application-layer secrets at rest — and one fewer operational key to manage; the setting's integration-specific NAME is cosmetic debt, noted, not worth a migration of the env contract now) and `mfa_activated_at TIMESTAMPTZ`. Secret present + `mfa_activated_at` NULL = enrollment pending (not yet enforced at login); both set = active. No separate table: it is 1:1 user state with no history requirement.
5. **Replay guard**: `totp_last_used_step BIGINT NULL` on `users` — a successfully used code's timestep is recorded, and any code from a timestep `<=` the recorded one is refused. Prevents replaying an intercepted code inside its validity window; costs one column and one comparison. Verification accepts `valid_window=1` (±30s clock skew).
6. **Flows:**
   - `POST /auth/mfa/enroll` (authenticated): 409 if MFA already activated (disable first); otherwise generate a fresh secret, store encrypted (overwriting any earlier un-activated one), return `{secret, otpauth_uri}` — the ONLY time the secret is presentable. `Cache-Control: no-store`.
   - `POST /auth/mfa/activate` (authenticated): `{totp_code}` — verified against the pending secret; sets `mfa_activated_at`; audit row `auth.mfa_activated`. 400 if no enrollment is pending; 401 on a wrong code.
   - `POST /auth/mfa/disable` (authenticated): `{current_password, totp_code}` — BOTH factors required, so a hijacked 15-minute access token alone cannot strip MFA. Clears all three columns; revokes every refresh token the user holds (same posture as change-password — user-confirmed 2026-07-16, during implementation review: disabling MFA is a security-posture downgrade exactly like a password change, and every route requiring proof of both factors is this codebase's established trigger for "force re-authentication everywhere"); audit row `auth.mfa_disabled`.
   - **Login**: `LoginRequest` gains optional `totp_code`. Password is verified FIRST (unchanged timing-equalized path); only for a password-valid user with active MFA is the code checked: missing → 401 `"TOTP code required"` (distinct detail — it leaks MFA-enabled only to a caller who already proved the password, and the client needs it to prompt), wrong/replayed → 401 `"Invalid TOTP code"`. Success records the used timestep.
   - **Refresh**: NO TOTP — rotation continues an already-MFA-proven session; the refresh token is the credential. (Decision, matching industry practice.)
   - **Change-password**: if MFA is active, `totp_code` becomes required there too (optional field, enforced conditionally) — otherwise a hijacked access token plus a shoulder-surfed password could rotate the password and then disable MFA.
7. **Recovery codes: deferred.** Losing the authenticator with no recovery path = lockout; v1's operational answer for this self-hosted deployment is an owner-role SQL reset (`UPDATE users SET totp_secret_encrypted = NULL, mfa_activated_at = NULL, totp_last_used_step = NULL WHERE email = ...`), documented here deliberately. One-time recovery codes are the natural follow-up scope.
8. **Audit**: `auth.mfa_activated` and `auth.mfa_disabled` (company-scoped via the actor's default membership, `_default_membership` reuse). Failed TOTP attempts at login are NOT audited (same rationale as failed passwords — volume noise; the lockout/rate-limiting story is one deferred item for both).
9. **MFA service module** `app/services/mfa.py` owns secret generation, otpauth URI construction (`issuer="Builders Stream"`, account = user email), verification + replay-step bookkeeping. Routes stay thin, mirroring `refresh_tokens.py`.

## Data model (migration 0015)

```sql
ALTER TABLE users ADD COLUMN totp_secret_encrypted TEXT;
ALTER TABLE users ADD COLUMN mfa_activated_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN totp_last_used_step BIGINT;
-- No RLS change (users has none); no new grants needed (0001 blanket).
```

## API changes

| Route | Change |
|---|---|
| `POST /auth/mfa/enroll` (new) | Authenticated. 200 `{secret, otpauth_uri}`; 409 if already active. no-store. |
| `POST /auth/mfa/activate` (new) | Authenticated. `{totp_code}` → 204; 400 no pending enrollment; 401 bad code. |
| `POST /auth/mfa/disable` (new) | Authenticated. `{current_password, totp_code}` → 204, revokes all refresh tokens; 401 either factor wrong. |
| `POST /auth/login` | Optional `totp_code`; conditional 401s per Decision 6. `TokenResponse` gains `mfa_enrollment_required: bool`. |
| `POST /auth/change-password` | Optional `totp_code`, required (401) when MFA active. |

None of the new routes takes `block_if_read_only` or `require_module` (session/credential management, same rationale as the token-lifecycle routes — the read-only completeness test's exclusion list gains the three MFA routes with this rationale).

## Testing strategy

One new file `backend/tests/test_mfa_totp.py` (route-level, `pyotp.TOTP(secret).now()` generates real codes): enroll→activate happy path (+ otpauth URI shape, audit row); enroll while active → 409; activate with no enrollment → 400; wrong activate code → 401; login without/with/with-wrong code across active-MFA states (including that a pre-activation pending enrollment does NOT gate login); replay of the same code → 401 (second use); disable requires both factors (each alone → 401), clears state, audit row; change-password gains the conditional TOTP requirement (without code → 401 when active; works when supplied); `mfa_enrollment_required` flag true for a fresh admin, false after activation, false for a non-admin (via invitation flow if cheap, else owner-DSN role edit); refresh works mid-session without any code. Existing suite must stay green untouched (all new request fields optional, response field additive).

E2E: enroll→activate→re-login-with-code→disable in Company A's block (pyotp available to the script? The script deliberately avoids backend imports but pyotp is a third-party lib installable in the host env — replicate inline like jwt is already used there).

## Deferred (explicitly out of scope)

- Hard admin enforcement / enrollment-limbo tokens (Decision 3).
- Recovery codes (Decision 7 documents the owner-SQL reset path).
- Rate limiting / failed-attempt lockout for TOTP and refresh alike.
- Renaming `integration_token_encryption_key` to something generic.
- Frontend enrollment UX (no app UI exists).
