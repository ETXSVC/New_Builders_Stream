# Auth Token Lifecycle Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement docs/07 Section 1's token lifecycle: 15-minute access tokens, opaque hashed refresh tokens with rotation + family reuse detection, and server-side revocation via `/auth/logout` and `/auth/change-password`.

**Architecture:** A new user-scoped (NO RLS) `refresh_tokens` table stores SHA-256 hashes of opaque secrets; a single service module (`app/services/refresh_tokens.py`) owns mint/rotate/revoke; three new `/auth` routes plus a widened `TokenResponse`. Access tokens stay stateless HS256 JWTs.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic, PyJWT, `secrets`/`hashlib` stdlib. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md` — read it before starting any task.

**Worktree:** `D:\Development\New const proj mgt software\.worktrees\auth-hardening`, branch `feature/auth-hardening`. All commands run from `<worktree>/backend` unless noted. Local test infra: the MAIN repo's compose project provides postgres (5432) / redis (6379) — verify with `docker exec newconstprojmgtsoftware-postgres-1 pg_isready -U postgres` before the first test run.

**House rules that apply to every task here:**
- One pytest session at a time — a second concurrent session poisons the shared `builders_stream_test` DB.
- Never assert a bare substring like `"Invalid"` when a more specific message exists; assert the full detail string.
- Comments state constraints the code can't show; match the file's existing density.

---

### Task 6.1: Config — 15-minute access tokens + refresh lifetime setting

**Files:**
- Modify: `backend/app/config.py` (line ~15)
- Modify: `.env.example` (repo root), `.env` (repo root, gitignored — update but NEVER commit)
- Test: `backend/tests/test_auth_token_lifecycle.py` (new file, first test)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_auth_token_lifecycle.py`:

```python
"""Auth token lifecycle (docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md).

Covers: access-token lifetime honoring settings (Task 6.1), refresh-token
issue/rotation/reuse-detection (Tasks 6.4-6.5), logout (6.6), and
change-password revoke-all (6.7). One file for the whole feature, same
convention as test_tier_gating.py.
"""
import uuid

import jwt as pyjwt
import pytest


def _register_payload():
    uid = uuid.uuid4().hex[:8]
    return {
        "company_name": f"TokenCo {uid}",
        "admin_full_name": "Toni Token",
        "admin_email": f"toni-{uid}@tokenco.test",
        "admin_password": "correct-horse-9",
    }


async def _register_and_login(client) -> dict:
    """Returns {"email", "password", "company_id", "user_id", "login": <login response json>}."""
    payload = _register_payload()
    register = await client.post("/auth/register", json=payload)
    assert register.status_code == 201, register.text
    login = await client.post(
        "/auth/login",
        json={"email": payload["admin_email"], "password": payload["admin_password"]},
    )
    assert login.status_code == 200, login.text
    return {
        "email": payload["admin_email"],
        "password": payload["admin_password"],
        "company_id": register.json()["company_id"],
        "user_id": register.json()["user_id"],
        "login": login.json(),
    }


@pytest.mark.asyncio
async def test_access_token_lifetime_honors_settings(client):
    """exp - iat must equal settings.jwt_expire_minutes * 60 exactly — this
    guards the 15-minute production default without pinning the test to
    wall-clock time (conftest pins JWT_EXPIRE_MINUTES=60 for tests; the
    invariant under test is that whatever the setting says is what the
    token gets, which is the same code path the 15-minute default uses)."""
    from app.config import settings

    ctx = await _register_and_login(client)
    claims = pyjwt.decode(
        ctx["login"]["access_token"],
        options={"verify_signature": False},
    )
    assert claims["exp"] - claims["iat"] == settings.jwt_expire_minutes * 60
```

- [ ] **Step 2: Run it — should PASS already** (the invariant already holds at 60):
`python -m pytest tests/test_auth_token_lifecycle.py -q` → 1 passed. This test is a regression guard, not TDD red; that's deliberate — the behavior change in this task is a *default value*, which tests cannot see (conftest pins 60).

- [ ] **Step 3: Change the defaults**

In `backend/app/config.py`, change line ~15 and add the new setting directly below it:

```python
    # docs/07 Section 1: access tokens are short-lived; refresh tokens
    # (Task 6.2+) carry the long-lived session. 15 is the spec's number.
    jwt_expire_minutes: int = 15
    refresh_token_expire_days: int = 14
```

In `.env.example` AND `.env` (repo root): `JWT_EXPIRE_MINUTES=60` → `JWT_EXPIRE_MINUTES=15`. Do NOT add REFRESH_TOKEN_EXPIRE_DAYS to either (default is fine; only override-worthy settings live in .env — match the file's existing minimalism). **Never commit `.env`.**

- [ ] **Step 4: Full-file sanity**: `python -m pytest tests/test_auth_token_lifecycle.py tests/test_auth.py -q` (test_auth.py may not exist under that exact name — run `python -m pytest tests/ -q -k "auth"` instead if so). Expected: all pass.

- [ ] **Step 5: Commit**

```
git add backend/app/config.py .env.example backend/tests/test_auth_token_lifecycle.py
git commit -m "feat: drop access-token lifetime to 15 minutes, add refresh lifetime setting"
```

---

### Task 6.2: RefreshToken model + migration 0014

**Files:**
- Create: `backend/app/models/refresh_token.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/migrations/versions/0014_refresh_tokens.py`

- [ ] **Step 1: Model**

`backend/app/models/refresh_token.py`:

```python
import uuid
from datetime import datetime

from sqlalchemy import CHAR, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin, utcnow


class RefreshToken(Base, UUIDPKMixin):
    """One issued refresh token (docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md).

    token_hash is the SHA-256 hex of the opaque secret — the presentable
    secret is never stored anywhere. family_id groups a rotation chain
    (minted at login, inherited by every rotation successor) so that reuse
    of an already-rotated token can revoke the whole chain at once.
    User-scoped, NO RLS (like users itself): a refresh token belongs to a
    person, not a tenant, and the table is never readable through any API.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(CHAR(64), unique=True, nullable=False)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("refresh_tokens.id"), nullable=True
    )
```

Add to `backend/app/models/__init__.py`: import `from app.models.refresh_token import RefreshToken` (after the IntegrationSyncRecord import) and `"RefreshToken",` at the end of `__all__`.

- [ ] **Step 2: Migration**

`backend/migrations/versions/0014_refresh_tokens.py`:

```python
"""refresh_tokens: hashed opaque refresh tokens with rotation families.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-16

Per docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md.

Deliberately NO RLS on this table — it is user-scoped, not tenant-scoped
(one user can belong to several companies; the token belongs to the
person), exactly like `users` itself, and no API route ever reads it back.
Every query filters on an exact token_hash or user_id server-side.

app_user keeps SELECT/INSERT/UPDATE from 0001's ALTER DEFAULT PRIVILEGES
but loses DELETE: revocation is an UPDATE (revoked_at), and revoked rows
are retained as evidence — same append-only posture as audit_log, applied
with REVOKE like 0006 did for esignatures.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.CHAR(64), nullable=False, unique=True),
        sa.Column("family_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "replaced_by_id",
            UUID(as_uuid=True),
            sa.ForeignKey("refresh_tokens.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])
    op.execute("REVOKE DELETE ON refresh_tokens FROM app_user")


def downgrade() -> None:
    op.drop_table("refresh_tokens")
```

**Before writing it, verify the 0006 REVOKE precedent's exact phrasing** (`Select-String -Path migrations\versions\0006*.py -Pattern "REVOKE"`) and match it. Also verify 0001 actually grants via ALTER DEFAULT PRIVILEGES (the 0013 docstring says so; confirm, don't trust).

- [ ] **Step 3: Apply + verify**: `python -m alembic upgrade head` against the LOCAL DEV database is not how this project verifies migrations — the test suite recreates `builders_stream_test` and upgrades to head on every run. So: `python -m pytest tests/test_auth_token_lifecycle.py -q` → passes, which proves 0014 applies cleanly. Additionally verify the grant: run a quick owner-DSN check that `DELETE FROM refresh_tokens` as app_user fails:

```
python -c "import asyncio, asyncpg;
async def main():
    conn = await asyncpg.connect('postgresql://app_user:app_password@localhost:5432/builders_stream_test')
    try:
        await conn.execute('DELETE FROM refresh_tokens')
        print('FAIL: delete allowed')
    except asyncpg.exceptions.InsufficientPrivilegeError:
        print('OK: delete denied')
    finally:
        await conn.close()
asyncio.run(main())"
```
Expected: `OK: delete denied`. (Write it as a scratch file if the inline quoting fights PowerShell; delete the scratch file after.)

- [ ] **Step 4: Commit**

```
git add backend/app/models/refresh_token.py backend/app/models/__init__.py backend/migrations/versions/0014_refresh_tokens.py
git commit -m "feat: refresh_tokens table + model (migration 0014, no RLS, no app_user DELETE)"
```

---

### Task 6.3: Refresh-token service

**Files:**
- Create: `backend/app/services/refresh_tokens.py`

No standalone unit tests in this task — every function is exercised through the route tests of 6.4–6.7 (same route-level-testing convention the rest of the codebase uses). The task still must pass `python -c "from app.services.refresh_tokens import mint_refresh_token"` and the full existing suite must stay green.

- [ ] **Step 1: Write the service**

`backend/app/services/refresh_tokens.py`:

```python
"""Refresh-token lifecycle (docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md).

The ONLY module that ever sees a presentable refresh-token secret. Routes
stay thin: they call these functions and map RefreshTokenError to a
uniform 401 "Invalid refresh token" (one message for unknown, expired,
revoked, and reuse-detected alike — no oracle distinguishing them).
"""
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import RefreshToken


class RefreshTokenError(Exception):
    """Presented token is not acceptable (unknown, expired, or revoked)."""


class RefreshTokenReuseError(RefreshTokenError):
    """Presented token was already rotated or revoked — treated as suspected
    compromise per the spec: the raiser has ALREADY revoked the whole family
    by the time this propagates. Carries user_id/family_id so the route can
    write the audit row before returning 401."""

    def __init__(self, user_id: uuid.UUID, family_id: uuid.UUID) -> None:
        super().__init__("refresh token reuse detected")
        self.user_id = user_id
        self.family_id = family_id


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


async def mint_refresh_token(
    session: AsyncSession, user_id: uuid.UUID, family_id: uuid.UUID | None = None
) -> tuple[RefreshToken, str]:
    """Returns (row, presentable_secret). family_id=None mints a new family
    (login); passing one keeps the rotation chain (rotate)."""
    secret = secrets.token_urlsafe(32)
    row = RefreshToken(
        user_id=user_id,
        token_hash=_hash(secret),
        family_id=family_id or uuid.uuid4(),
        expires_at=datetime.now(timezone.utc)
        + timedelta(days=settings.refresh_token_expire_days),
    )
    session.add(row)
    await session.flush()
    return row, secret


async def find_by_secret(session: AsyncSession, secret: str) -> RefreshToken | None:
    result = await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == _hash(secret))
    )
    return result.scalar_one_or_none()


async def revoke_family(session: AsyncSession, family_id: uuid.UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )


async def revoke_all_for_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )


async def rotate_refresh_token(
    session: AsyncSession, presented_secret: str
) -> tuple[RefreshToken, str]:
    """Single-use rotation. Returns (old_row, new_presentable_secret); the
    successor row is flushed in old_row.family_id's chain and old_row is
    marked revoked + replaced_by. Raises RefreshTokenReuseError (after
    revoking the family) if the token was already rotated/revoked, plain
    RefreshTokenError if unknown or expired."""
    row = await find_by_secret(session, presented_secret)
    if row is None:
        raise RefreshTokenError("unknown refresh token")
    if row.revoked_at is not None or row.replaced_by_id is not None:
        # Reuse of a spent token = suspected compromise. Kill the chain
        # BEFORE raising; the route must let this UPDATE commit (the 401
        # must not roll back the containment — see the /auth/refresh route).
        await revoke_family(session, row.family_id)
        raise RefreshTokenReuseError(user_id=row.user_id, family_id=row.family_id)
    if row.expires_at <= datetime.now(timezone.utc):
        raise RefreshTokenError("expired refresh token")
    new_row, new_secret = await mint_refresh_token(
        session, user_id=row.user_id, family_id=row.family_id
    )
    row.revoked_at = datetime.now(timezone.utc)
    row.replaced_by_id = new_row.id
    await session.flush()
    return row, new_secret
```

- [ ] **Step 2: Import check + suite spot-check**: `python -c "from app.services.refresh_tokens import rotate_refresh_token"` then `python -m pytest tests/test_auth_token_lifecycle.py -q`.

- [ ] **Step 3: Commit**

```
git add backend/app/services/refresh_tokens.py
git commit -m "feat: refresh-token service - mint, rotate, family revocation, reuse detection"
```

---

### Task 6.4: Login issues a refresh token

**Files:**
- Modify: `backend/app/schemas/auth.py` (TokenResponse)
- Modify: `backend/app/routers/auth.py` (login)
- Test: `backend/tests/test_auth_token_lifecycle.py`

- [ ] **Step 1: Failing tests**

Append to `backend/tests/test_auth_token_lifecycle.py`:

```python
@pytest.mark.asyncio
async def test_login_returns_a_refresh_token(client):
    ctx = await _register_and_login(client)
    body = ctx["login"]
    assert "refresh_token" in body, body
    assert isinstance(body["refresh_token"], str) and len(body["refresh_token"]) >= 32
    assert body["refresh_token"] != body["access_token"]


@pytest.mark.asyncio
async def test_stored_refresh_token_is_hashed_not_plaintext(client):
    """Owner-DSN check (same direct-DB test-setup precedent as
    set_subscription_tier): the DB row holds a 64-char hex SHA-256, never
    the presentable secret."""
    import asyncpg

    from tests.conftest import TEST_DATABASE_URL

    ctx = await _register_and_login(client)
    secret = ctx["login"]["refresh_token"]
    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        rows = await conn.fetch(
            "SELECT token_hash FROM refresh_tokens WHERE user_id = $1",
            __import__("uuid").UUID(ctx["user_id"]),
        )
    finally:
        await conn.close()
    assert len(rows) == 1
    stored = rows[0]["token_hash"]
    assert len(stored) == 64 and all(c in "0123456789abcdef" for c in stored)
    assert stored != secret
```

Run: `python -m pytest tests/test_auth_token_lifecycle.py -q` → the two new tests FAIL (no refresh_token key).

- [ ] **Step 2: Widen TokenResponse**

In `backend/app/schemas/auth.py`:

```python
class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    default_company_id: uuid.UUID
```

- [ ] **Step 3: Rewrite login to mint a family**

In `backend/app/routers/auth.py`: add `from app.services.refresh_tokens import mint_refresh_token` to the imports, then change the login body's tail. The mint is an INSERT, and `session_scope()` does NOT commit on exit — so the write portion needs an explicit transaction. Replace lines from the membership lookup comment through the return with:

```python
        # Membership lookup needs app.current_user_id set for the self_membership
        # RLS policy to allow it (design decision #3).
        await set_current_user(session, str(user.id))
        result = await session.execute(
            select(CompanyUser)
            .where(CompanyUser.user_id == user.id)
            # (keep the existing ordering comment verbatim)
            .order_by(CompanyUser.created_at, CompanyUser.company_id)
        )
        membership = result.scalars().first()
        if membership is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "User has no company memberships")

        # Refresh-token INSERT needs a commit; session_scope() never commits
        # on its own (register's explicit session.begin() is the precedent).
        # set_current_user above used set_config(..., is_local=true), which
        # is transaction-scoped — SQLAlchemy autobegan a transaction for it,
        # so commit via the session, not a nested begin().
        _, refresh_secret = await mint_refresh_token(session, user_id=user.id)
        await session.commit()

        token = create_access_token(user_id=str(user.id), default_company_id=str(membership.company_id))
        return TokenResponse(
            access_token=token,
            refresh_token=refresh_secret,
            default_company_id=membership.company_id,
        )
```

**Verify the autobegin claim empirically, don't trust the comment**: if `await session.commit()` errors with "no transaction is begun" or the row doesn't persist, restructure to wrap the whole body in `async with session.begin():` (dropping the explicit commit) — whichever the codebase's SQLAlchemy version actually supports. The test from Step 1 is the arbiter (the owner-DSN read happens over a separate connection, so it only sees COMMITTED rows — it cannot pass on an uncommitted insert).

- [ ] **Step 4: Run the new tests, then the whole file, then the auth-adjacent suite**:
`python -m pytest tests/test_auth_token_lifecycle.py -q` → all pass.
`python -m pytest tests/ -q -k "auth or login or register or invitation"` → all pass (every login-consuming test keeps working because the response change is additive).

- [ ] **Step 5: Commit**

```
git add backend/app/schemas/auth.py backend/app/routers/auth.py backend/tests/test_auth_token_lifecycle.py
git commit -m "feat: login mints a refresh-token family and returns the secret"
```

---

### Task 6.5: POST /auth/refresh — rotation, reuse detection, membership re-derivation

**Files:**
- Modify: `backend/app/schemas/auth.py` (RefreshRequest)
- Modify: `backend/app/routers/auth.py` (new route)
- Test: `backend/tests/test_auth_token_lifecycle.py`

- [ ] **Step 1: Failing tests**

Append:

```python
async def _refresh(client, refresh_token: str):
    return await client.post("/auth/refresh", json={"refresh_token": refresh_token})


@pytest.mark.asyncio
async def test_refresh_rotates_and_rederives_company(client):
    ctx = await _register_and_login(client)
    first = ctx["login"]
    r = await _refresh(client, first["refresh_token"])
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["access_token"] != first["access_token"]
    assert second["refresh_token"] != first["refresh_token"]
    assert second["default_company_id"] == str(first["default_company_id"]) or second[
        "default_company_id"
    ] == first["default_company_id"]

    # the rotated-away token is spent: a second use is refused
    dead = await _refresh(client, first["refresh_token"])
    assert dead.status_code == 401
    assert dead.json()["detail"] == "Invalid refresh token"


@pytest.mark.asyncio
async def test_rotation_chain_each_token_works_exactly_once(client):
    ctx = await _register_and_login(client)
    tokens = [ctx["login"]["refresh_token"]]
    for _ in range(2):
        r = await _refresh(client, tokens[-1])
        assert r.status_code == 200, r.text
        tokens.append(r.json()["refresh_token"])
    assert len(set(tokens)) == 3


@pytest.mark.asyncio
async def test_reuse_of_a_spent_token_kills_the_whole_family(client):
    """The reuse-detection core: after A -> B rotation, presenting A again
    must 401 AND kill B (the legitimate successor) — and the containment
    must SURVIVE the 401 (i.e. the family revocation commits even though
    the request errors). Also asserts the audit row."""
    import asyncpg

    from tests.conftest import TEST_DATABASE_URL

    ctx = await _register_and_login(client)
    token_a = ctx["login"]["refresh_token"]
    token_b = (await _refresh(client, token_a)).json()["refresh_token"]

    reused = await _refresh(client, token_a)
    assert reused.status_code == 401
    assert reused.json()["detail"] == "Invalid refresh token"

    survivor = await _refresh(client, token_b)
    assert survivor.status_code == 401, (
        "the legitimate successor must be dead after reuse detection: "
        f"{survivor.status_code}: {survivor.text}"
    )

    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        audit = await conn.fetch(
            "SELECT action FROM audit_log WHERE action = 'auth.refresh_reuse_detected'"
        )
    finally:
        await conn.close()
    assert len(audit) == 1, f"expected exactly one reuse audit row, got {len(audit)}"


@pytest.mark.asyncio
async def test_expired_refresh_token_is_refused(client):
    """Back-date expires_at via owner DSN (same direct-DB setup precedent
    as set_subscription_tier)."""
    import asyncpg

    from tests.conftest import TEST_DATABASE_URL

    ctx = await _register_and_login(client)
    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        result = await conn.execute(
            "UPDATE refresh_tokens SET expires_at = now() - interval '1 day' WHERE user_id = $1",
            __import__("uuid").UUID(ctx["user_id"]),
        )
        assert result == "UPDATE 1", result
    finally:
        await conn.close()
    r = await _refresh(client, ctx["login"]["refresh_token"])
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid refresh token"


@pytest.mark.asyncio
async def test_garbage_refresh_token_is_refused(client):
    r = await _refresh(client, "not-a-real-token")
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid refresh token"
```

Run → all new tests FAIL (404, no route).

- [ ] **Step 2: Schema**

In `backend/app/schemas/auth.py`:

```python
class RefreshRequest(BaseModel):
    refresh_token: str
```

- [ ] **Step 3: Route**

In `backend/app/routers/auth.py` (imports: add `RefreshRequest` to the schemas import, `write_audit_log` is already imported, and extend the service import to `from app.services.refresh_tokens import RefreshTokenError, RefreshTokenReuseError, mint_refresh_token, rotate_refresh_token`):

```python
@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest) -> TokenResponse:
    """Rotate a refresh token (docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md).

    Reuse of a spent token is suspected compromise: the service revokes the
    whole family, and this route must COMMIT that revocation before the 401
    leaves — an exception inside the transaction would roll the containment
    back, which is why the error is carried out of the session block in a
    flag instead of raised inside it. All failure modes share one message
    (no oracle for which of unknown/expired/revoked/reused it was).
    """
    reuse_detected = False
    async with session_scope() as session:
        try:
            old_row, new_secret = await rotate_refresh_token(session, payload.refresh_token)
        except RefreshTokenReuseError as exc:
            # Audit needs a company scope; resolve it the same way login
            # resolves default_company_id. If the user has no memberships
            # left, skip the row (there is no company to file it under) —
            # the family revocation itself still commits.
            await set_current_user(session, str(exc.user_id))
            result = await session.execute(
                select(CompanyUser)
                .where(CompanyUser.user_id == exc.user_id)
                .order_by(CompanyUser.created_at, CompanyUser.company_id)
            )
            membership = result.scalars().first()
            if membership is not None:
                await set_current_tenant(session, str(membership.company_id))
                await write_audit_log(
                    session,
                    company_id=membership.company_id,
                    actor_id=exc.user_id,
                    action="auth.refresh_reuse_detected",
                    entity_type="refresh_token",
                    entity_id=exc.family_id,
                    metadata={"family_id": str(exc.family_id)},
                )
            await session.commit()
            reuse_detected = True
        except RefreshTokenError:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
        else:
            await set_current_user(session, str(old_row.user_id))
            result = await session.execute(
                select(CompanyUser)
                .where(CompanyUser.user_id == old_row.user_id)
                .order_by(CompanyUser.created_at, CompanyUser.company_id)
            )
            membership = result.scalars().first()
            if membership is None:
                # Same outcome login gives a membership-less user. The
                # rotation rolls back with the session (never committed),
                # so the presented token remains usable if memberships
                # are later restored.
                raise HTTPException(status.HTTP_403_FORBIDDEN, "User has no company memberships")
            await session.commit()
            access = create_access_token(
                user_id=str(old_row.user_id), default_company_id=str(membership.company_id)
            )
            company_id = membership.company_id
    if reuse_detected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
    return TokenResponse(
        access_token=access, refresh_token=new_secret, default_company_id=company_id
    )
```

Note `set_current_tenant` must be in the `app.db` import line already used by this file (it is — register uses it). The audit_log INSERT is RLS-protected (tenant-scoped table), hence the `set_current_tenant` call before `write_audit_log`.

- [ ] **Step 4: Run**: `python -m pytest tests/test_auth_token_lifecycle.py -q` → all pass. Then `python -m pytest tests/test_audit_log.py -q` (audit completeness conventions may enumerate actions — if it fails, read its failure and extend its expected-actions table per its own documented maintenance contract).

- [ ] **Step 5: Commit**

```
git add backend/app/schemas/auth.py backend/app/routers/auth.py backend/tests/test_auth_token_lifecycle.py
git commit -m "feat: POST /auth/refresh with rotation and family reuse detection"
```

---

### Task 6.6: POST /auth/logout

**Files:**
- Modify: `backend/app/routers/auth.py`
- Test: `backend/tests/test_auth_token_lifecycle.py`

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.asyncio
async def test_logout_revokes_the_family_and_is_idempotent(client):
    ctx = await _register_and_login(client)
    token = ctx["login"]["refresh_token"]

    out = await client.post("/auth/logout", json={"refresh_token": token})
    assert out.status_code == 204

    dead = await _refresh(client, token)
    assert dead.status_code == 401

    again = await client.post("/auth/logout", json={"refresh_token": token})
    assert again.status_code == 204  # idempotent, not an oracle

    garbage = await client.post("/auth/logout", json={"refresh_token": "nope"})
    assert garbage.status_code == 204
```

Run → FAIL (404).

- [ ] **Step 2: Route** (service import gains `find_by_secret, revoke_family`):

```python
@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: RefreshRequest) -> None:
    """Revoke the presented token's whole rotation family. Possession of
    the refresh token is the credential (same no-bearer reasoning as the
    OAuth callback and invitation-accept routes). Always 204 — a logout
    endpoint must not be a validity oracle, so unknown/spent tokens
    succeed silently."""
    async with session_scope() as session:
        row = await find_by_secret(session, payload.refresh_token)
        if row is not None:
            await revoke_family(session, row.family_id)
            await session.commit()
```

- [ ] **Step 3: Run the file** → all pass.

- [ ] **Step 4: Commit**

```
git add backend/app/routers/auth.py backend/tests/test_auth_token_lifecycle.py
git commit -m "feat: POST /auth/logout revokes the refresh-token family, idempotently"
```

---

### Task 6.7: POST /auth/change-password

**Files:**
- Modify: `backend/app/schemas/auth.py`
- Modify: `backend/app/routers/auth.py`
- Test: `backend/tests/test_auth_token_lifecycle.py`

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.asyncio
async def test_change_password_revokes_everything_and_rotates_credentials(client):
    import asyncpg

    from tests.conftest import TEST_DATABASE_URL

    ctx = await _register_and_login(client)
    # a second login = a second family; change-password must kill BOTH
    second_login = await client.post(
        "/auth/login", json={"email": ctx["email"], "password": ctx["password"]}
    )
    assert second_login.status_code == 200
    headers = {"Authorization": f"Bearer {ctx['login']['access_token']}"}

    wrong = await client.post(
        "/auth/change-password",
        json={"current_password": "not-the-password", "new_password": "brand-new-pass-1"},
        headers=headers,
    )
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == "Invalid current password"
    # a wrong guess must NOT revoke anything
    still_alive = await _refresh(client, second_login.json()["refresh_token"])
    assert still_alive.status_code == 200, still_alive.text

    ok = await client.post(
        "/auth/change-password",
        json={"current_password": ctx["password"], "new_password": "brand-new-pass-1"},
        headers=headers,
    )
    assert ok.status_code == 204

    # every refresh token from before the change is dead (both families)
    dead1 = await _refresh(client, ctx["login"]["refresh_token"])
    dead2 = await _refresh(client, still_alive.json()["refresh_token"])
    assert dead1.status_code == 401 and dead2.status_code == 401

    # old password refused, new one works
    old = await client.post(
        "/auth/login", json={"email": ctx["email"], "password": ctx["password"]}
    )
    assert old.status_code == 401
    new = await client.post(
        "/auth/login", json={"email": ctx["email"], "password": "brand-new-pass-1"}
    )
    assert new.status_code == 200, new.text

    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        audit = await conn.fetch(
            "SELECT action FROM audit_log WHERE action = 'auth.password_changed'"
        )
    finally:
        await conn.close()
    assert len(audit) == 1


@pytest.mark.asyncio
async def test_change_password_enforces_min_length(client):
    ctx = await _register_and_login(client)
    headers = {"Authorization": f"Bearer {ctx['login']['access_token']}"}
    r = await client.post(
        "/auth/change-password",
        json={"current_password": ctx["password"], "new_password": "short"},
        headers=headers,
    )
    assert r.status_code == 422
```

Run → FAIL (404).

- [ ] **Step 2: Schema**

```python
class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)
```

- [ ] **Step 3: Route.** This one is authenticated — find how other routers declare the dependency (`from app.core.deps import get_current_user, CurrentUser`; look at any gated route for the exact parameter idiom) and use `current: CurrentUser = Depends(get_current_user)`. No role gate (self-service), no `block_if_read_only` (a read-only company's users must still be able to rotate a compromised password), no tier gate.

```python
@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: ChangePasswordRequest,
    current: CurrentUser = Depends(get_current_user),
) -> None:
    """Verify-then-rehash, then revoke EVERY refresh token the user holds
    across all families/devices (docs/07 Section 1's 'password change'
    revocation trigger). current_password is required so a hijacked
    15-minute access token alone cannot rotate the password. A wrong
    current_password revokes nothing — an attacker guessing must not be
    able to DoS the real user's sessions."""
    session = current.session
    result = await session.execute(select(User).where(User.id == current.user_id))
    user = result.scalar_one()
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid current password")
    user.password_hash = hash_password(payload.new_password)
    await revoke_all_for_user(session, current.user_id)
    await write_audit_log(
        session,
        company_id=current.company_id,
        actor_id=current.user_id,
        action="auth.password_changed",
        entity_type="user",
        entity_id=current.user_id,
        metadata=None,
    )
```

**Check how `get_current_user`-provided sessions commit** — read `app/core/deps.py` first. If the dependency wraps the request in a transaction that commits on success (likely, given every other mutating route writes through it without explicit commits), no commit call is needed here; otherwise add one, mirroring whatever an existing mutating route (e.g. `create_invoice`) does. Adapt `current.user_id`/`current.company_id` attribute names to CurrentUser's actual fields — read the dataclass, don't guess.

- [ ] **Step 4: Run the file, then the invitations/companies suites** (they exercise get_current_user paths): `python -m pytest tests/test_auth_token_lifecycle.py tests/test_invitations.py -q` (adjust filename to what exists).

- [ ] **Step 5: Commit**

```
git add backend/app/schemas/auth.py backend/app/routers/auth.py backend/tests/test_auth_token_lifecycle.py
git commit -m "feat: POST /auth/change-password with revoke-all and audit trail"
```

---

### Task 6.8: Documentation sync

**Files:**
- Modify: `docs/05-api-specification.md` (routes table, Section 1)
- Modify: `docs/07-security-compliance.md` (Section 1)

- [ ] **Step 1:** In docs/05's auth table (lines ~22-23), add three rows after `/auth/login`:

```markdown
| `/auth/refresh` | POST | Rotate a refresh token; reuse of a spent token revokes its whole family | Refresh token |
| `/auth/logout` | POST | Revoke the presented refresh token's family (idempotent, always 204) | Refresh token |
| `/auth/change-password` | POST | Verify current password, set new one, revoke all the user's refresh tokens | Current + new password (authenticated) |
```

- [ ] **Step 2:** In docs/07 Section 1, append one sentence to the second bullet noting implementation: refresh-token rotation with family reuse detection is implemented per `docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md` (access 15 min, refresh 14 days absolute), and MFA/TOTP remains the section's one open item (its own follow-up spec). Match the tier-gating cross-reference style already present in Section 2.

- [ ] **Step 3: Commit**

```
git add docs/05-api-specification.md docs/07-security-compliance.md
git commit -m "docs: record token-lifecycle routes in API spec and security plan"
```

---

### Task 6.9: E2E extension + full regression + closeout + PR

**Files:**
- Modify: `scripts/e2e_smoke_test.py`
- Modify: `docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md` (closeout note)

- [ ] **Step 1: E2E.** Read the script's Company A block; immediately after Company A's login, insert a token-lifecycle sequence (adapting variable names to the block's):

1. Assert login body now has `refresh_token` → checks_passed.
2. `POST /auth/refresh` with it → 200, different tokens → checks_passed.
3. Re-present the ORIGINAL (now spent) token → 401 → checks_passed ("reuse detection live").
4. The successor from step 2 is now also dead (family killed) → 401 → checks_passed.
5. Log in AGAIN (fresh family — the reuse probe deliberately burned the first one) and continue the block with the new access token. Add a comment stating why the re-login exists.
6. `POST /auth/logout` with the new refresh token → 204, then refresh → 401 → checks_passed. Then log in a THIRD time for the remainder of the block (comment: the block's later steps and the pre-existing checks need a live token; logout killed the second).

Simpler alternative the implementer may choose (document which): run the whole lifecycle sequence with a dedicated re-login at the END of Company A's block, so the block's pre-existing flow is untouched and only ONE extra login is needed. Prefer this if it reads cleaner.

Add a Task 6.9 module-docstring note. NOTE: every E2E block's login now returns a refresh_token nobody uses — that's additive and harmless; do not thread refresh handling through other blocks.

- [ ] **Step 2: Live verification.** Same procedure as tier gating's Task 5.10: stop the main-repo compose project (`docker compose down`, NO -v, from the main repo — report that it needs restarting), `docker compose up -d --build` from THIS worktree, `pg_isready`, apply migrations host-side if the volume is fresh (`cd backend; python -m alembic upgrade head` with MIGRATIONS_DATABASE_URL pointing at the compose DB), `curl http://localhost:8000/health`, run `python scripts/e2e_smoke_test.py`. All pre-existing checks + the new ones must pass. The frontend port-3001 failure at the very end is pre-existing/environmental — extract checks_passed via the frame-recovery precedent and report success-with-known-issue. Bring the stack down after (no -v); restart the main-repo postgres/redis.

- [ ] **Step 3: Full regression, twice, solo** (controller runs these, not a subagent): `cd backend && python -m pytest -q` ×2 (~19 min each), plus the 8-file RLS suite (same file list as tier gating's closeout). Expected: everything green; new-test count raises the total above 743.

- [ ] **Step 4: Closeout note** atop the spec (Implementation Status paragraph, prior specs' convention): completion, both pass counts/timings, RLS count, E2E additions, deliberately-not-fixed items. Commit: `docs: close out auth token-lifecycle implementation`.

- [ ] **Step 5: Push + PR.**

```
git push -u origin feature/auth-hardening
gh pr create --base main --head feature/auth-hardening --title "feat: auth token lifecycle - 15-min access tokens, refresh rotation, revocation" --body-file <scratchpad>/pr-body.md
```

(Write the body to the scratchpad first — embedded quotes break PowerShell native-arg quoting; this exact failure happened on PR #12.)

Confirm CI goes green. **Merging remains an explicit, separate user decision — not automatic.**
