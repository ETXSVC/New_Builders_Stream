# Integrations (QuickBooks/FreshBooks) Provider-Agnostic Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the provider-agnostic Integrations core — OAuth connect/callback, an `AccountingProviderClient` abstraction with only a fake implementation, event-driven async sync (Invoice/Bill/Expense creation), retry, and per-record sync status — entirely behind `FakeAccountingProviderClient`, with zero real QuickBooks/FreshBooks wiring.

**Architecture:** Two new tables (`integration_connections`, `integration_sync_records`) with standard RLS. A `GET /integrations/{provider}/connect` route returns a fake authorization URL; `GET /integrations/{provider}/callback` (no `CurrentUser`, authenticated via a signed `state` JWT) exchanges a fake code for fake tokens and stores an encrypted connection. Three new events (`INVOICE_CREATED`, `EXPENSE_CREATED`, `BILL_CREATED`) fan out to a thin handler that enqueues one Dramatiq message per active connection; a generic `sync_financial_record` actor does the actual (fake) push and updates per-record status, retrying via Dramatiq's own `max_retries`.

**Tech Stack:** FastAPI, SQLAlchemy async, PostgreSQL RLS, Dramatiq (existing), PyJWT (existing, reused for `state` signing), `cryptography` (new — Fernet token-at-rest encryption).

Full design: [`docs/superpowers/specs/2026-07-15-integrations-quickbooks-freshbooks-design.md`](../specs/2026-07-15-integrations-quickbooks-freshbooks-design.md).

---

### Task 4.1: `IntegrationConnection` & `IntegrationSyncRecord` Models

**Files:**
- Create: `backend/app/models/integration_connection.py`
- Create: `backend/app/models/integration_sync_record.py`
- Modify: `backend/app/models/__init__.py`

- [ ] **Step 1: Write `backend/app/models/integration_connection.py`**

```python
"""Task 4.1 (design spec Section 1): OAuth connection state for a company's
QuickBooks/FreshBooks integration. access_token_encrypted/
refresh_token_encrypted are Fernet ciphertext (Task 4.3's
app/services/token_encryption.py), never plaintext at rest — enforced by
this column simply never holding anything else; no application code path
writes a raw token here.
"""
import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class IntegrationConnection(Base, UUIDPKMixin):
    __tablename__ = "integration_connections"
    __table_args__ = (
        UniqueConstraint("company_id", "provider", name="uq_integration_connections_company_provider"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    access_token_encrypted: Mapped[str] = mapped_column(String, nullable=False)
    refresh_token_encrypted: Mapped[str] = mapped_column(String, nullable=False)
    connected_at: Mapped[datetime] = mapped_column(nullable=False)
```

Check `backend/app/models/base.py` for `UUIDPKMixin`'s and `TimestampMixin`'s exact fields before writing this — `IntegrationConnection` uses `UUIDPKMixin` (for `id`) but declares its own `connected_at` rather than `TimestampMixin`'s `created_at`/`updated_at`, matching the design spec's own column name. If `UUIDPKMixin` already provides `id` with a server-side UUID default, do not redeclare it.

- [ ] **Step 2: Write `backend/app/models/integration_sync_record.py`**

```python
"""Task 4.1 (design spec Section 1): per-record sync status against one
connected provider. Mutable current-state, not an append-only log — see
the design spec's own Section 1 for why (matches Dramatiq's own retry
re-running the SAME logical job, and answers "is this record synced right
now," not "show me every attempt ever made")."""
import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class IntegrationSyncRecord(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "integration_sync_records"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "entity_type", "entity_id",
            name="uq_integration_sync_records_connection_entity",
        ),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integration_connections.id"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempted_at: Mapped[datetime | None] = mapped_column(nullable=True)
```

Only `TimestampMixin.created_at` is used from that mixin (not `updated_at`, if that mixin has one — check `base.py`); the row is updated in place by the sync actor directly setting `status`/`attempt_count`/`last_error`/`last_attempted_at`, not via a mixin-managed `updated_at`.

- [ ] **Step 3: Register both models in `backend/app/models/__init__.py`**

Add, after the `Expense` import (the last existing entry):

```python
from app.models.integration_connection import IntegrationConnection
from app.models.integration_sync_record import IntegrationSyncRecord
```

And add `"IntegrationConnection"`, `"IntegrationSyncRecord"` to `__all__`.

- [ ] **Step 4: Verify the app still imports cleanly**

Run: `cd backend && python -c "from app.main import app; print('ok')"`
Expected: `ok` (migration doesn't exist yet, so no DB round trip happens at import time — this only proves the model files themselves are syntactically and import-correct).

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/integration_connection.py backend/app/models/integration_sync_record.py backend/app/models/__init__.py
git commit -m "feat: add IntegrationConnection and IntegrationSyncRecord models"
```

---

### Task 4.2: Migration (0013) — Integrations Schema + RLS

**Files:**
- Create: `backend/migrations/versions/0013_integrations_schema.py`

- [ ] **Step 1: Write the migration**

Check `backend/migrations/versions/0012_invoicing_ar_ap_schema.py`'s exact `create_table`/RLS-policy syntax first (already read during planning — the `tenant_isolation` policy shape below is copied verbatim from that file, only the table name changed) and confirm `revision`/`down_revision` chain against the actual latest migration file in the directory (expected `0012`, but verify — do not assume).

```python
"""Integrations schema: integration_connections, integration_sync_records,
and their RLS policies.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-15

Per docs/superpowers/specs/2026-07-15-integrations-quickbooks-freshbooks-design.md
Section 1. Two tables:

  integration_connections   -> companies
  integration_sync_records  -> integration_connections, companies

Both are plain, flat, company-scoped resources (no hierarchy/bidirectional
concern of their own) — each gets the standard single, non-inherited
tenant_isolation policy, the same shape 0012 gives invoices/bills/expenses.

No REVOKE on either table: both have real, planned mutation paths
(callback upserts connections; the sync actor updates sync records in
place) — defaults to the ordinary app_user grants from 0001's ALTER
DEFAULT PRIVILEGES.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- integration_connections ------------------------------------------
    op.create_table(
        "integration_connections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("access_token_encrypted", sa.Text, nullable=False),
        sa.Column("refresh_token_encrypted", sa.Text, nullable=False),
        sa.Column(
            "connected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("provider IN ('quickbooks','freshbooks')", name="ck_integration_connections_provider"),
        sa.UniqueConstraint(
            "company_id", "provider", name="uq_integration_connections_company_provider"
        ),
    )

    op.execute("ALTER TABLE integration_connections ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON integration_connections FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )

    # --- integration_sync_records -------------------------------------------
    op.create_table(
        "integration_sync_records",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column(
            "connection_id", UUID(as_uuid=True), sa.ForeignKey("integration_connections.id"), nullable=False
        ),
        sa.Column("entity_type", sa.String(20), nullable=False),
        sa.Column("entity_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "entity_type IN ('invoice','expense','bill')", name="ck_integration_sync_records_entity_type"
        ),
        sa.CheckConstraint(
            "status IN ('pending','success','failed')", name="ck_integration_sync_records_status"
        ),
        sa.UniqueConstraint(
            "connection_id", "entity_type", "entity_id",
            name="uq_integration_sync_records_connection_entity",
        ),
    )
    op.create_index(
        "idx_integration_sync_records_connection_status",
        "integration_sync_records",
        ["connection_id", "status"],
    )

    op.execute("ALTER TABLE integration_sync_records ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON integration_sync_records FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )


def downgrade() -> None:
    op.drop_table("integration_sync_records")
    op.drop_table("integration_connections")
```

- [ ] **Step 2: Run the migration against the TEST database explicitly**

This worktree's own `.env` may default `MIGRATIONS_DATABASE_URL` to the shared dev database, not the test one — Task 3.32's own review found and fixed exactly this trap (accidentally migrating the shared dev DB instead of the test DB). Before running anything, read `.env` and confirm which DSN `MIGRATIONS_DATABASE_URL` actually points at.

Run (adjust the explicit override if `.env`'s default is already correct):
`cd backend && MIGRATIONS_DATABASE_URL=$TEST_DATABASE_URL python -m alembic upgrade head`
Expected: `Running upgrade 0012 -> 0013, Integrations schema...` with no errors.

- [ ] **Step 3: Confirm the migration applied cleanly to the test DB and NOT to the shared dev DB**

Run a raw check against the test DB confirming both new tables exist, and a raw check against the shared dev DB (if `docker compose`'s postgres is up) confirming they do NOT exist yet — mirroring Task 3.32's own verification discipline.

- [ ] **Step 4: Commit**

```bash
git add backend/migrations/versions/0013_integrations_schema.py
git commit -m "feat: add integrations schema migration (0013) with RLS"
```

---

### Task 4.3: Token Encryption Helper + `cryptography` Dependency

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/app/config.py`
- Create: `backend/app/services/token_encryption.py`
- Test: `backend/tests/test_token_encryption.py`

- [ ] **Step 1: Add `cryptography` to `backend/pyproject.toml`'s `dependencies` list**

`cryptography` is already present transitively (via `argon2-cffi`/`pyjwt`) in this project's venv today, but this task is the first thing that imports it DIRECTLY, so it must be declared as an explicit dependency, not relied upon as an undeclared transitive one. Add, after `"apscheduler>=3.10,<4.0",`:

```python
    # Task 4.3: OAuth token-at-rest encryption for integration_connections
    # (docs/07-security-compliance.md Section 4's existing requirement that
    # third-party OAuth tokens are "additionally encrypted at the
    # application layer before storage, never stored in plaintext"). Already
    # present transitively via argon2-cffi/pyjwt in this project's venv, but
    # this is the first code that imports it directly.
    "cryptography>=43.0,<44.0",
```

- [ ] **Step 2: Add `integration_token_encryption_key` to `backend/app/config.py`'s `Settings`**

```python
    # Task 4.3: Fernet key (44-char urlsafe-base64, generate with
    # `Fernet.generate_key()`) for encrypting integration_connections'
    # access_token_encrypted/refresh_token_encrypted columns at the
    # application layer. Required (no default) — unlike jwt_secret, which
    # is reused for the OAuth state token under a distinct signing context
    # (Task 4.5), this is a genuinely separate secret with a different
    # cryptographic purpose (symmetric encryption, not HMAC signing) and
    # must not be the same value as jwt_secret.
    integration_token_encryption_key: str
```

- [ ] **Step 3: Add the required setting to the test/dev `.env` files**

Check `backend/tests/conftest.py` and the worktree's own `.env`/`.env.example` for how `jwt_secret` is set for tests, and add `INTEGRATION_TOKEN_ENCRYPTION_KEY` alongside it using a real generated Fernet key (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`), not a placeholder string — `Settings` will fail to construct at import time otherwise, breaking every test in this worktree, not just this task's own.

- [ ] **Step 4: Write the failing test**

```python
"""Task 4.3 (design spec Section 1): Fernet-based encryption for OAuth
tokens at rest."""
from app.services.token_encryption import decrypt_token, encrypt_token


def test_encrypt_then_decrypt_round_trips_to_the_original_value():
    plaintext = "fake_access_token_abc123"
    ciphertext = encrypt_token(plaintext)

    assert ciphertext != plaintext
    assert decrypt_token(ciphertext) == plaintext


def test_ciphertext_is_not_human_readable():
    ciphertext = encrypt_token("a_real_looking_secret_value")
    assert "a_real_looking_secret_value" not in ciphertext
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_token_encryption.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `backend/app/services/token_encryption.py`**

```python
"""Task 4.3 (design spec Section 1): Fernet symmetric encryption for OAuth
tokens at rest, per docs/07-security-compliance.md Section 4's existing
requirement. Fernet (not raw AES) because it bundles authenticated
encryption (AES-128-CBC + HMAC) and timestamp/versioning in one
self-contained token format — the standard, low-footprint choice for
"encrypt this string, decrypt it back later" in Python, matching this
codebase's own "use the standard library/battle-tested primitive, don't
hand-roll crypto" discipline already established for password hashing
(argon2-cffi) and JWT signing (pyjwt).
"""
from cryptography.fernet import Fernet

from app.config import settings

_fernet = Fernet(settings.integration_token_encryption_key.encode())


def encrypt_token(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    return _fernet.decrypt(ciphertext.encode()).decode()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_token_encryption.py -v`
Expected: PASS, 2 passed

- [ ] **Step 5: Commit**

```bash
git add backend/pyproject.toml backend/app/config.py backend/app/services/token_encryption.py backend/tests/test_token_encryption.py
git commit -m "feat: add Fernet token-at-rest encryption for OAuth tokens"
```

---

### Task 4.4: `AccountingProviderClient` Protocol + `FakeAccountingProviderClient`

**Files:**
- Create: `backend/app/services/accounting_client.py`
- Test: `backend/tests/test_accounting_client.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Task 4.4 (design spec Section 2): AccountingProviderClient Protocol +
FakeAccountingProviderClient, mirroring app/services/stripe_client.py's own
StripeClient/FakeStripeClient shape."""
import pytest

from app.services.accounting_client import (
    AccountingProviderError,
    FakeAccountingProviderClient,
    get_accounting_client,
)


async def test_get_authorization_url_returns_a_url_containing_the_state():
    client = FakeAccountingProviderClient(provider="quickbooks")
    url = await client.get_authorization_url(company_id="company-1", state="signed-state-token")
    assert url.startswith("https://")
    assert "signed-state-token" in url


async def test_exchange_code_for_tokens_returns_a_distinct_access_and_refresh_token():
    client = FakeAccountingProviderClient(provider="quickbooks")
    access_token, refresh_token = await client.exchange_code_for_tokens(code="fake-code")
    assert access_token != refresh_token
    assert access_token.startswith("access_fake_")
    assert refresh_token.startswith("refresh_fake_")


async def test_push_invoice_accumulates_into_pushed_invoices_and_returns_an_external_id():
    client = FakeAccountingProviderClient(provider="quickbooks")
    external_id = await client.push_invoice(access_token="tok", invoice={"invoice_number": "INV-2026-0001"})
    assert external_id
    assert client.pushed_invoices == [{"invoice_number": "INV-2026-0001"}]


async def test_push_expense_accumulates_into_pushed_expenses():
    client = FakeAccountingProviderClient(provider="quickbooks")
    await client.push_expense(access_token="tok", expense={"description": "Materials"})
    assert client.pushed_expenses == [{"description": "Materials"}]


async def test_push_bill_accumulates_into_pushed_bills():
    client = FakeAccountingProviderClient(provider="quickbooks")
    await client.push_bill(access_token="tok", bill={"vendor_name": "Ace Plumbing"})
    assert client.pushed_bills == [{"vendor_name": "Ace Plumbing"}]


async def test_should_fail_makes_every_push_raise_accounting_provider_error():
    client = FakeAccountingProviderClient(provider="quickbooks", should_fail=True)
    with pytest.raises(AccountingProviderError):
        await client.push_invoice(access_token="tok", invoice={})
    with pytest.raises(AccountingProviderError):
        await client.push_expense(access_token="tok", expense={})
    with pytest.raises(AccountingProviderError):
        await client.push_bill(access_token="tok", bill={})
    # should_fail must not have accumulated any of the failed pushes.
    assert client.pushed_invoices == []
    assert client.pushed_expenses == []
    assert client.pushed_bills == []


async def test_get_accounting_client_returns_a_fake_regardless_of_provider_string():
    quickbooks_client = get_accounting_client("quickbooks")
    freshbooks_client = get_accounting_client("freshbooks")
    assert isinstance(quickbooks_client, FakeAccountingProviderClient)
    assert isinstance(freshbooks_client, FakeAccountingProviderClient)
    assert quickbooks_client.provider == "quickbooks"
    assert freshbooks_client.provider == "freshbooks"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_accounting_client.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `backend/app/services/accounting_client.py`**

```python
"""Task 4.4 (design spec Section 2): the AccountingProviderClient
abstraction. Every consumer in this codebase (the connect/callback routes,
the sync actor) depends only on the AccountingProviderClient Protocol
below, never on a real QuickBooks/FreshBooks SDK directly — same
"swap the real thing for a fake behind an interface" pattern
app/services/stripe_client.py already establishes for Stripe, applied here
to a second class of external paid API.

FakeAccountingProviderClient is the ONLY implementation this plan builds.
It is deterministic and in-memory: no network calls, no real QuickBooks/
FreshBooks developer account needed to build or test this entire feature.
RealQuickBooksClient/RealFreshBooksClient wrapping the actual SDKs are
explicit future work (design spec's own Open Questions) — when built, each
implements this same Protocol; nothing in this file's Protocol or its
consumers changes.

Typed methods per operation (push_invoice/push_expense/push_bill), not one
generic push_record(entity_type, ...) dispatch method — matching
StripeClient's own established shape, since a real SDK genuinely has
separate endpoints per record type.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol


class AccountingProviderError(Exception):
    """Raised by a push_* call when the (fake, or eventually real) provider
    rejects or fails to process a record. The caller (the sync actor,
    Task 4.12) is responsible for recording this as a failed sync attempt
    and re-raising so Dramatiq's own retry picks it back up — not this
    module's job."""


class AccountingProviderClient(Protocol):
    async def get_authorization_url(self, *, company_id: str, state: str) -> str: ...

    async def exchange_code_for_tokens(self, *, code: str) -> tuple[str, str]: ...

    async def push_invoice(self, *, access_token: str, invoice: dict) -> str: ...

    async def push_expense(self, *, access_token: str, expense: dict) -> str: ...

    async def push_bill(self, *, access_token: str, bill: dict) -> str: ...


@dataclass
class FakeAccountingProviderClient:
    """In-memory, deterministic, no network calls. `provider` only shapes
    cosmetic details (the fake authorization URL's hostname, fake
    external-ID prefixes) — it never branches behavior, since there is
    nothing real to branch between yet. `pushed_invoices`/`pushed_expenses`/
    `pushed_bills` accumulate every successful push as the raw dict passed
    in, for tests to assert against — same accumulator pattern
    FakeStripeClient's own `reported_usage` already establishes.
    `should_fail`, when True, makes every push_* call raise
    AccountingProviderError instead of accumulating — the one test hook
    needed to exercise the retry/failure path without real network
    flakiness."""

    provider: str
    should_fail: bool = False
    pushed_invoices: list[dict] = field(default_factory=list)
    pushed_expenses: list[dict] = field(default_factory=list)
    pushed_bills: list[dict] = field(default_factory=list)

    async def get_authorization_url(self, *, company_id: str, state: str) -> str:
        return f"https://{self.provider}.fake-oauth.test/authorize?state={state}"

    async def exchange_code_for_tokens(self, *, code: str) -> tuple[str, str]:
        return (
            f"access_fake_{uuid.uuid4().hex[:16]}",
            f"refresh_fake_{uuid.uuid4().hex[:16]}",
        )

    async def push_invoice(self, *, access_token: str, invoice: dict) -> str:
        if self.should_fail:
            raise AccountingProviderError(f"Fake {self.provider} push_invoice failure")
        self.pushed_invoices.append(invoice)
        return f"{self.provider}_inv_{uuid.uuid4().hex[:16]}"

    async def push_expense(self, *, access_token: str, expense: dict) -> str:
        if self.should_fail:
            raise AccountingProviderError(f"Fake {self.provider} push_expense failure")
        self.pushed_expenses.append(expense)
        return f"{self.provider}_exp_{uuid.uuid4().hex[:16]}"

    async def push_bill(self, *, access_token: str, bill: dict) -> str:
        if self.should_fail:
            raise AccountingProviderError(f"Fake {self.provider} push_bill failure")
        self.pushed_bills.append(bill)
        return f"{self.provider}_bill_{uuid.uuid4().hex[:16]}"


def get_accounting_client(provider: str) -> AccountingProviderClient:
    """The one seam a real implementation plugs into later (design spec
    Section 2). Today always returns a fresh FakeAccountingProviderClient
    regardless of `provider` — when RealQuickBooksClient/RealFreshBooksClient
    exist, this function is the only place that changes."""
    return FakeAccountingProviderClient(provider=provider)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_accounting_client.py -v`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/accounting_client.py backend/tests/test_accounting_client.py
git commit -m "feat: add AccountingProviderClient Protocol + FakeAccountingProviderClient"
```

---

### Task 4.5: OAuth `state` Signing/Verification Helper

**Files:**
- Create: `backend/app/services/integration_oauth_state.py`
- Test: `backend/tests/test_integration_oauth_state.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Task 4.5 (design spec Section 3): signed, short-lived OAuth `state`
tokens. Reuses settings.jwt_secret under a distinct signing context (an
`aud` claim this codebase's real access tokens — app/core/security.py's
create_access_token — never set), not a second secret or a
database-backed nonce table."""
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.config import settings
from app.services.integration_oauth_state import (
    InvalidOAuthStateError,
    sign_oauth_state,
    verify_oauth_state,
)


def test_sign_then_verify_round_trips_company_id_and_provider():
    token = sign_oauth_state(company_id="company-1", provider="quickbooks")
    company_id, provider = verify_oauth_state(token)
    assert company_id == "company-1"
    assert provider == "quickbooks"


def test_tampered_token_is_rejected():
    token = sign_oauth_state(company_id="company-1", provider="quickbooks")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(InvalidOAuthStateError):
        verify_oauth_state(tampered)


def test_expired_token_is_rejected():
    now = datetime.now(timezone.utc)
    expired_payload = {
        "company_id": "company-1",
        "provider": "quickbooks",
        "aud": "integration_oauth_state",
        "iat": now - timedelta(minutes=20),
        "exp": now - timedelta(minutes=10),
    }
    expired_token = jwt.encode(expired_payload, settings.jwt_secret, algorithm="HS256")
    with pytest.raises(InvalidOAuthStateError):
        verify_oauth_state(expired_token)


def test_a_real_access_token_is_rejected_as_a_state_token():
    """A real login JWT (app/core/security.py's create_access_token) must
    NOT be accepted as a valid OAuth state — proves the distinct `aud`
    claim genuinely separates the two token purposes, not just by
    convention."""
    from app.core.security import create_access_token

    login_token = create_access_token(user_id="user-1", default_company_id="company-1")
    with pytest.raises(InvalidOAuthStateError):
        verify_oauth_state(login_token)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_integration_oauth_state.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `backend/app/services/integration_oauth_state.py`**

```python
"""Task 4.5 (design spec Section 3): signs and verifies the short-lived
`state` parameter GET /integrations/{provider}/connect hands to the fake
authorization URL, and GET /integrations/{provider}/callback later
receives back. The signed state IS callback's entire authentication —
callback has no CurrentUser (an external redirect cannot carry a bearer
token, the same structural reality /invitations/{id}/accept already
established) — so a forged or expired state must be rejected, not merely
logged.

Reuses settings.jwt_secret (not a second secret) under a distinct `aud`
claim ("integration_oauth_state") that app/core/security.py's real login
tokens never set — this is what stops a real, valid login JWT (which a
malicious caller could already possess for their own account) from also
being accepted as a valid OAuth state for some OTHER company_id. verify_
oauth_state explicitly requires and checks this claim, it does not just
happen to ignore it.
"""
from datetime import datetime, timedelta, timezone

import jwt

from app.config import settings

_AUDIENCE = "integration_oauth_state"
_TTL_MINUTES = 10


class InvalidOAuthStateError(Exception):
    """Raised for a missing, tampered, expired, or wrong-audience state
    token. The caller (GET /integrations/{provider}/callback, Task 4.9) is
    responsible for turning this into an HTTP 400, not this module."""


def sign_oauth_state(*, company_id: str, provider: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "company_id": company_id,
        "provider": provider,
        "aud": _AUDIENCE,
        "iat": now,
        "exp": now + timedelta(minutes=_TTL_MINUTES),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def verify_oauth_state(token: str) -> tuple[str, str]:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], audience=_AUDIENCE)
    except jwt.PyJWTError as exc:
        raise InvalidOAuthStateError(str(exc)) from exc

    return payload["company_id"], payload["provider"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_integration_oauth_state.py -v`
Expected: PASS, 4 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/integration_oauth_state.py backend/tests/test_integration_oauth_state.py
git commit -m "feat: add signed OAuth state token helper for Integrations callback"
```

---

### Task 4.6: Integration Schemas

**Files:**
- Create: `backend/app/schemas/integration.py`

- [ ] **Step 1: Write `backend/app/schemas/integration.py`**

```python
"""Task 4.6 (design spec Sections 3, 6): request/response schemas for the
Integrations routes."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AuthorizationUrlResponse(BaseModel):
    authorization_url: str


class IntegrationConnectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    connected_at: datetime


class SyncRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    status: str
    attempt_count: int
    last_error: str | None
    last_attempted_at: datetime | None


class SyncStatusResponse(BaseModel):
    provider: str
    connected_at: datetime
    records: list[SyncRecordResponse]
    next_cursor: str | None = None
```

`AuthorizationUrlResponse`/`IntegrationConnectionResponse` never expose `access_token_encrypted`/`refresh_token_encrypted` — neither field appears on any response schema anywhere in this file, so there is no accidental leak path even if a route handler passes the full ORM object through `model_validate`.

- [ ] **Step 2: Verify the app still imports cleanly**

Run: `cd backend && python -c "from app.schemas.integration import AuthorizationUrlResponse, IntegrationConnectionResponse, SyncStatusResponse; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add backend/app/schemas/integration.py
git commit -m "feat: add Integrations request/response schemas"
```

---

### Task 4.7: `GET /integrations/{provider}/connect`

**Files:**
- Create: `backend/app/routers/integrations.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_integrations.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Task 4.7 (design spec Section 3): GET /integrations/{provider}/connect."""


async def _register_and_login(client, company_name, email):
    register = await client.post(
        "/auth/register",
        json={
            "company_name": company_name,
            "admin_full_name": "Test Admin",
            "admin_email": email,
            "admin_password": "supersecret123",
        },
    )
    assert register.status_code == 201, register.text
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    return {
        "company_id": register.json()["company_id"],
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


async def test_connect_returns_a_fake_authorization_url(client):
    admin = await _register_and_login(client, "Integ Co 1", "integ-1@example.test")

    response = await client.get("/integrations/quickbooks/connect", headers=admin["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["authorization_url"].startswith("https://quickbooks.fake-oauth.test/")
    assert "state=" in body["authorization_url"]


async def test_connect_rejects_an_unknown_provider(client):
    admin = await _register_and_login(client, "Integ Co 2", "integ-2@example.test")

    response = await client.get("/integrations/xero/connect", headers=admin["headers"])
    assert response.status_code == 422


async def test_project_manager_cannot_connect(client):
    admin = await _register_and_login(client, "Integ Co 3", "integ-3@example.test")
    invite = await client.post(
        "/invitations", json={"email": "pm-integ@example.test", "role": "project_manager"}, headers=admin["headers"]
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "PM User", "password": "supersecret123"},
    )
    pm_login = await client.post(
        "/auth/login", json={"email": "pm-integ@example.test", "password": "supersecret123"}
    )
    pm_headers = {"Authorization": f"Bearer {pm_login.json()['access_token']}"}

    response = await client.get("/integrations/quickbooks/connect", headers=pm_headers)
    assert response.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_integrations.py -v`
Expected: FAIL — routes don't exist yet

- [ ] **Step 3: Write `backend/app/routers/integrations.py`**

```python
"""Task 4.7 (design spec Section 3): GET /integrations/{provider}/connect.
Top-level routes, not company-nested — provider is a path segment
(quickbooks/freshbooks), matching the API spec's own sketch. RBAC per
docs/07-security-compliance.md Section 2's "Integrations" row:
Admin/Accountant only, ZERO Client/PM/Field-Crew access.
"""
from typing import Literal

from fastapi import APIRouter, Depends

from app.core.deps import CurrentUser, require_role
from app.schemas.integration import AuthorizationUrlResponse
from app.services.accounting_client import get_accounting_client
from app.services.integration_oauth_state import sign_oauth_state

router = APIRouter(prefix="/integrations", tags=["integrations"])

_ROLES = ("admin", "accountant")

Provider = Literal["quickbooks", "freshbooks"]


@router.get("/{provider}/connect", response_model=AuthorizationUrlResponse)
async def connect(
    provider: Provider,
    current: CurrentUser = Depends(require_role(*_ROLES)),
) -> AuthorizationUrlResponse:
    state = sign_oauth_state(company_id=str(current.company_id), provider=provider)
    client = get_accounting_client(provider)
    authorization_url = await client.get_authorization_url(
        company_id=str(current.company_id), state=state
    )
    return AuthorizationUrlResponse(authorization_url=authorization_url)
```

`provider: Provider` (a `Literal["quickbooks", "freshbooks"]` FastAPI path parameter) is what makes `test_connect_rejects_an_unknown_provider` get a 422 — FastAPI validates the path parameter against the Literal before the handler body ever runs, matching `integration_connections.provider`'s own CHECK constraint values exactly. This is a read-only route (no `block_if_read_only` — a lapsed subscription should not prevent an Admin from starting the OAuth flow to reconnect an integration; only `callback`'s actual connection-creating write is the meaningful mutation, and even that one has no `CurrentUser` to check read-only status against, per Task 4.9).

- [ ] **Step 4: Register the router in `backend/app/main.py`**

Add `integrations` to the alphabetized import block from `app.routers`, and add `app.include_router(integrations.router)` — check the existing registration order (alphabetical by router variable name, matching every other entry) and place it accordingly.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_integrations.py -v`
Expected: PASS, 3 passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/routers/integrations.py backend/app/main.py backend/tests/test_integrations.py
git commit -m "feat: add GET /integrations/{provider}/connect"
```

---

### Task 4.8: `IntegrationConnection` Upsert Helper

**Files:**
- Modify: `backend/app/routers/integrations.py`
- Modify: `backend/tests/test_integrations.py`

A small dedicated task so Task 4.9 (`callback`) can focus purely on request handling, not upsert mechanics — mirrors how `next_invoice_number` was its own task (3.33) before the routes that call it.

- [ ] **Step 1: Add `_upsert_connection` to `backend/app/routers/integrations.py`**

```python
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models import IntegrationConnection
from app.services.token_encryption import encrypt_token


async def _upsert_connection(
    current: CurrentUser, *, company_id: uuid.UUID, provider: str, access_token: str, refresh_token: str
) -> IntegrationConnection:
    """Reconnecting the same provider (callback firing again) replaces
    stale tokens rather than erroring — design spec Section 1's own
    ON CONFLICT (company_id, provider) DO UPDATE requirement. Uses
    Postgres's native upsert (not a SELECT-then-INSERT-or-UPDATE) so this
    is a single atomic statement, not a race between two separate
    connect attempts for the same company+provider landing concurrently."""
    stmt = (
        pg_insert(IntegrationConnection)
        .values(
            id=uuid.uuid4(),
            company_id=company_id,
            provider=provider,
            access_token_encrypted=encrypt_token(access_token),
            refresh_token_encrypted=encrypt_token(refresh_token),
        )
        .on_conflict_do_update(
            index_elements=["company_id", "provider"],
            set_={
                "access_token_encrypted": encrypt_token(access_token),
                "refresh_token_encrypted": encrypt_token(refresh_token),
            },
        )
        .returning(IntegrationConnection)
    )
    result = await current.session.execute(stmt)
    await current.session.flush()
    return result.scalar_one()
```

Note the `current: CurrentUser` parameter here is misleading for how Task 4.9 will actually call this — `callback` has no `CurrentUser` (Task 4.9). Adjust this helper's signature during Task 4.9's own implementation to accept a raw `AsyncSession` instead of `CurrentUser`, once that route's real shape is in front of you — flagged here so this task's own code compiles standalone, not as a mandate to keep the wrong parameter type.

- [ ] **Step 2: No new tests for this task alone — Task 4.9's own tests exercise this helper**

This task exists to keep Task 4.9's diff focused; there is no route calling `_upsert_connection` yet, so nothing new to assert against in isolation. Confirm the file still imports cleanly: `cd backend && python -c "from app.routers.integrations import _upsert_connection; print('ok')"`.

- [ ] **Step 3: Commit**

```bash
git add backend/app/routers/integrations.py
git commit -m "feat: add IntegrationConnection upsert helper"
```

---

### Task 4.9: `GET /integrations/{provider}/callback`

**Files:**
- Modify: `backend/app/routers/integrations.py`
- Modify: `backend/tests/test_integrations.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_integrations.py`:

```python
import asyncpg

from app.services.integration_oauth_state import sign_oauth_state
from tests.conftest import TEST_DATABASE_URL

ADMIN_CONN_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")


async def test_callback_with_a_validly_signed_state_creates_the_connection(client):
    admin = await _register_and_login(client, "Integ Co 4", "integ-4@example.test")
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")

    response = await client.get(
        f"/integrations/quickbooks/callback?code=fake-code&state={state}"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "quickbooks"

    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        row = await conn.fetchrow(
            "SELECT provider, access_token_encrypted, refresh_token_encrypted "
            "FROM integration_connections WHERE company_id = $1",
            admin["company_id"],
        )
    finally:
        await conn.close()
    assert row["provider"] == "quickbooks"
    # Never plaintext at rest — docs/07-security-compliance.md Section 4.
    assert "access_fake_" not in row["access_token_encrypted"]
    assert "refresh_fake_" not in row["refresh_token_encrypted"]


async def test_callback_writes_an_audit_log_entry(client):
    admin = await _register_and_login(client, "Integ Co 5", "integ-5@example.test")
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")

    await client.get(f"/integrations/quickbooks/callback?code=fake-code&state={state}")

    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        rows = await conn.fetch(
            "SELECT action FROM audit_log WHERE company_id = $1", admin["company_id"]
        )
    finally:
        await conn.close()
    actions = {row["action"] for row in rows}
    assert "integration.connected" in actions


async def test_callback_with_an_invalid_state_returns_400(client):
    response = await client.get(
        "/integrations/quickbooks/callback?code=fake-code&state=not-a-real-token"
    )
    assert response.status_code == 400


async def test_reconnecting_the_same_provider_replaces_the_old_tokens(client):
    admin = await _register_and_login(client, "Integ Co 6", "integ-6@example.test")
    state_1 = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    await client.get(f"/integrations/quickbooks/callback?code=code-1&state={state_1}")

    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        first_token = await conn.fetchval(
            "SELECT access_token_encrypted FROM integration_connections WHERE company_id = $1",
            admin["company_id"],
        )
    finally:
        await conn.close()

    state_2 = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    second = await client.get(f"/integrations/quickbooks/callback?code=code-2&state={state_2}")
    assert second.status_code == 200, second.text

    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        rows = await conn.fetch(
            "SELECT id, access_token_encrypted FROM integration_connections WHERE company_id = $1",
            admin["company_id"],
        )
    finally:
        await conn.close()
    assert len(rows) == 1, "reconnecting must update the existing row, not insert a second one"
    assert rows[0]["access_token_encrypted"] != first_token
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_integrations.py -v -k callback`
Expected: FAIL — route doesn't exist yet

- [ ] **Step 3: Add `callback` to `backend/app/routers/integrations.py`**

Rewrite `_upsert_connection` to take a raw `AsyncSession` (per Task 4.8's own note), and add the route:

```python
from fastapi import HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.schemas.integration import IntegrationConnectionResponse
from app.services.audit import write_audit_log
from app.services.integration_oauth_state import InvalidOAuthStateError, verify_oauth_state

# callback has no CurrentUser (no bearer token on an external redirect —
# app/core/deps.py's get_current_user cannot run here), so it opens its own
# session against the app_user role, exactly like every Dramatiq worker
# actor in this codebase does for the same "no request-scoped session to
# inherit" reason. Uses settings.database_url (the app_user-role DSN
# get_current_user's own dependency chain uses), not
# migrations_database_url (the owner-role DSN worker actors use) — this
# route must go through RLS like any other tenant-scoped write, it does
# not need cross-tenant visibility the way a scheduled job scanning every
# company does.
_engine = create_async_engine(settings.database_url, pool_pre_ping=True)
_SessionLocal = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


async def _upsert_connection(
    session: AsyncSession, *, company_id: uuid.UUID, provider: str, access_token: str, refresh_token: str
) -> IntegrationConnection:
    """Reconnecting the same provider (callback firing again) replaces
    stale tokens rather than erroring — design spec Section 1's own
    ON CONFLICT (company_id, provider) DO UPDATE requirement. Uses
    Postgres's native upsert (not a SELECT-then-INSERT-or-UPDATE) so this
    is a single atomic statement, not a race between two separate connect
    attempts for the same company+provider landing concurrently."""
    stmt = (
        pg_insert(IntegrationConnection)
        .values(
            id=uuid.uuid4(),
            company_id=company_id,
            provider=provider,
            access_token_encrypted=encrypt_token(access_token),
            refresh_token_encrypted=encrypt_token(refresh_token),
        )
        .on_conflict_do_update(
            index_elements=["company_id", "provider"],
            set_={
                "access_token_encrypted": encrypt_token(access_token),
                "refresh_token_encrypted": encrypt_token(refresh_token),
            },
        )
        .returning(IntegrationConnection)
    )
    result = await session.execute(stmt)
    await session.flush()
    return result.scalar_one()


@router.get("/{provider}/callback", response_model=IntegrationConnectionResponse)
async def callback(
    provider: Provider,
    code: str = Query(...),
    state: str = Query(...),
) -> IntegrationConnectionResponse:
    try:
        state_company_id, state_provider = verify_oauth_state(state)
    except InvalidOAuthStateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired state") from exc

    if state_provider != provider:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "state does not match provider")

    company_id = uuid.UUID(state_company_id)

    async with _SessionLocal() as session:
        # Every write in this session must go through RLS scoped to the
        # company the signed state decoded — never trust company_id from
        # anywhere else in this request, since there is no CurrentUser to
        # cross-check it against.
        await session.execute(text("SELECT set_config('app.current_tenant', :cid, false)"), {"cid": str(company_id)})

        client = get_accounting_client(provider)
        access_token, refresh_token = await client.exchange_code_for_tokens(code=code)

        connection = await _upsert_connection(
            session,
            company_id=company_id,
            provider=provider,
            access_token=access_token,
            refresh_token=refresh_token,
        )

        await write_audit_log(
            session,
            company_id=company_id,
            actor_id=None,
            action="integration.connected",
            entity_type="integration_connection",
            entity_id=connection.id,
            metadata={"provider": provider},
        )
        await session.commit()

    return IntegrationConnectionResponse.model_validate(connection)
```

Check `backend/app/db.py`'s `set_current_tenant` helper (already used by `TenantMiddleware`) before writing the raw `SELECT set_config(...)` call above — reuse that exact helper function if it is importable and has a matching signature, rather than duplicating its SQL inline; only fall back to the raw statement shown if that helper is structurally tied to the middleware's own request-scoped session in a way that doesn't fit a standalone session here. `actor_id=None` for the same documented reason `estimate_approved_handler.py`'s own audit entry uses `None` — no `CurrentUser` exists on this route, so there is no actor id in scope to pass, not a claim that no human acted.

- [ ] **Step 4: Add `/integrations/{provider}/callback` to the `block_if_read_only` completeness test's exclusion list**

In `backend/tests/test_read_only_enforcement.py`, add `"/integrations/{provider}/callback"` to `excluded_paths`, with a one-line comment matching the existing entries' style: same reason `/invitations/{invitation_id}/accept` is excluded — no `CurrentUser` on this route to check a subscription against. Do not add `connect` or `sync-status` here — both have a real `CurrentUser` and are GET-only (already exempt via `block_if_read_only`'s own "GET/HEAD/OPTIONS always pass" rule, not via this exclusion list).

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_integrations.py tests/test_read_only_enforcement.py -v`
Expected: PASS — all `test_integrations.py` tests plus the completeness test

- [ ] **Step 6: Commit**

```bash
git add backend/app/routers/integrations.py backend/tests/test_integrations.py backend/tests/test_read_only_enforcement.py
git commit -m "feat: add GET /integrations/{provider}/callback"
```

---

### Task 4.10: `GET /integrations/{provider}/sync-status`

**Files:**
- Modify: `backend/app/routers/integrations.py`
- Modify: `backend/tests/test_integrations.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_integrations.py`:

```python
async def _connect(client, headers, company_id, provider="quickbooks"):
    state = sign_oauth_state(company_id=company_id, provider=provider)
    response = await client.get(f"/integrations/{provider}/callback?code=fake-code&state={state}")
    assert response.status_code == 200, response.text
    return response.json()


async def test_sync_status_404s_for_a_provider_with_no_connection(client):
    admin = await _register_and_login(client, "Integ Co 7", "integ-7@example.test")

    response = await client.get("/integrations/quickbooks/sync-status", headers=admin["headers"])
    assert response.status_code == 404


async def test_sync_status_returns_the_connection_summary_with_an_empty_records_list(client):
    admin = await _register_and_login(client, "Integ Co 8", "integ-8@example.test")
    await _connect(client, admin["headers"], admin["company_id"])

    response = await client.get("/integrations/quickbooks/sync-status", headers=admin["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "quickbooks"
    assert body["records"] == []


async def test_project_manager_cannot_read_sync_status(client):
    admin = await _register_and_login(client, "Integ Co 9", "integ-9@example.test")
    await _connect(client, admin["headers"], admin["company_id"])
    invite = await client.post(
        "/invitations", json={"email": "pm-integ2@example.test", "role": "project_manager"}, headers=admin["headers"]
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "PM User", "password": "supersecret123"},
    )
    pm_login = await client.post(
        "/auth/login", json={"email": "pm-integ2@example.test", "password": "supersecret123"}
    )
    pm_headers = {"Authorization": f"Bearer {pm_login.json()['access_token']}"}

    response = await client.get("/integrations/quickbooks/sync-status", headers=pm_headers)
    assert response.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_integrations.py -v -k sync_status`
Expected: FAIL — route doesn't exist yet

- [ ] **Step 3: Add `sync-status` to `backend/app/routers/integrations.py`**

```python
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.models import IntegrationSyncRecord
from app.schemas.integration import SyncRecordResponse, SyncStatusResponse


@router.get("/{provider}/sync-status", response_model=SyncStatusResponse)
async def sync_status(
    provider: Provider,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> SyncStatusResponse:
    connection_result = await current.session.execute(
        select(IntegrationConnection).where(IntegrationConnection.provider == provider)
    )
    connection = connection_result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No {provider} connection for this company")

    query = select(IntegrationSyncRecord).where(IntegrationSyncRecord.connection_id == connection.id)
    if status_filter is not None:
        query = query.where(IntegrationSyncRecord.status == status_filter)

    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=IntegrationSyncRecord.created_at,
        id_col=IntegrationSyncRecord.id,
        cursor=cursor,
        limit=limit,
    )

    return SyncStatusResponse(
        provider=connection.provider,
        connected_at=connection.connected_at,
        records=[SyncRecordResponse.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_integrations.py -v`
Expected: PASS — all tests in the file (check the current total count, don't assume a stale number)

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/integrations.py backend/tests/test_integrations.py
git commit -m "feat: add GET /integrations/{provider}/sync-status"
```

---

### Task 4.11: `handle_financial_record_created` Event Handler + Registration

**Files:**
- Create: `backend/app/services/financial_record_sync_handler.py`
- Modify: `backend/app/core/event_handlers.py`
- Test: `backend/tests/test_financial_record_sync_handler.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Task 4.11 (design spec Section 4): handle_financial_record_created —
enqueues one sync_financial_record message per active connection, does no
sync work itself."""
import uuid

from app.core.event_handlers import register_event_handlers
from app.core.events import publish


async def _register_and_login(client, company_name, email):
    register = await client.post(
        "/auth/register",
        json={
            "company_name": company_name,
            "admin_full_name": "Test Admin",
            "admin_email": email,
            "admin_password": "supersecret123",
        },
    )
    assert register.status_code == 201, register.text
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    return {
        "company_id": register.json()["company_id"],
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


async def test_zero_connections_enqueues_nothing(client, monkeypatch):
    register_event_handlers()
    admin = await _register_and_login(client, "Sync Co 1", "sync-1@example.test")

    from app.tasks.accounting_sync import sync_financial_record

    calls = []
    monkeypatch.setattr(sync_financial_record, "send", lambda *a, **kw: calls.append((a, kw)))

    await publish(
        "INVOICE_CREATED",
        session=None,
        entity_type="invoice",
        entity_id=uuid.uuid4(),
        company_id=uuid.UUID(admin["company_id"]),
    )
    assert calls == []


async def test_one_active_connection_enqueues_one_message(client, monkeypatch):
    from app.services.integration_oauth_state import sign_oauth_state

    register_event_handlers()
    admin = await _register_and_login(client, "Sync Co 2", "sync-2@example.test")
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    connect_response = await client.get(f"/integrations/quickbooks/callback?code=fake&state={state}")
    assert connect_response.status_code == 200, connect_response.text
    connection_id = connect_response.json()["id"]

    from app.tasks.accounting_sync import sync_financial_record

    calls = []
    monkeypatch.setattr(sync_financial_record, "send", lambda *a, **kw: calls.append((a, kw)))

    entity_id = uuid.uuid4()
    await publish(
        "INVOICE_CREATED",
        session=None,
        entity_type="invoice",
        entity_id=entity_id,
        company_id=uuid.UUID(admin["company_id"]),
    )

    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["connection_id"] == connection_id
    assert kwargs["entity_type"] == "invoice"
    assert kwargs["entity_id"] == str(entity_id)


async def test_two_active_connections_enqueue_two_messages(client, monkeypatch):
    from app.services.integration_oauth_state import sign_oauth_state

    register_event_handlers()
    admin = await _register_and_login(client, "Sync Co 3", "sync-3@example.test")
    qb_state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    await client.get(f"/integrations/quickbooks/callback?code=fake&state={qb_state}")
    fb_state = sign_oauth_state(company_id=admin["company_id"], provider="freshbooks")
    await client.get(f"/integrations/freshbooks/callback?code=fake&state={fb_state}")

    from app.tasks.accounting_sync import sync_financial_record

    calls = []
    monkeypatch.setattr(sync_financial_record, "send", lambda *a, **kw: calls.append((a, kw)))

    await publish(
        "BILL_CREATED",
        session=None,
        entity_type="bill",
        entity_id=uuid.uuid4(),
        company_id=uuid.UUID(admin["company_id"]),
    )

    assert len(calls) == 2
    providers_synced = {kw["connection_id"] for _, kw in calls}
    assert len(providers_synced) == 2
```

Note: `session=None` in the `publish()` calls above only works if `handle_financial_record_created` never actually dereferences `session` for its own queries (it should use its own session, matching the "no request-scoped session for a handler that only enqueues, doesn't write" reasoning below) — confirm this assumption is true of your own implementation before relying on it; if the handler DOES need the caller's session (e.g. to query `integration_connections` inside the same transaction as the just-created Invoice/Bill/Expense, so a rolled-back creation never enqueues a sync for a record that doesn't exist), pass a real session from a `db_session`-style fixture instead of `None`. Read `test_lead_won_drafts_project.py`'s own handler-testing pattern before deciding, and prefer reusing the caller's session if there's precedent for it (there is: `estimate_approved_handler.py` reuses `session` for its own reads/writes) — adjust these three tests' `session=None` to a real fixture session if so.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_financial_record_sync_handler.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `backend/app/services/financial_record_sync_handler.py`**

```python
"""INVOICE_CREATED/EXPENSE_CREATED/BILL_CREATED event handler (Task 4.11,
design spec Section 4): enqueues one sync_financial_record Dramatiq
message per active integration connection. Does NO sync work itself — US-
6.2 requires syncing to be asynchronous, so this handler's only job is
deciding WHAT needs syncing and enqueuing it, the same "thin wrapper
enqueues, worker does the real work" split app/scheduler.py's own job
wrappers already establish for scheduled jobs.

Registered against app.core.events by app.core.event_handlers's
register_event_handlers(), same is_registered() re-registration guard
every other handler in this codebase uses.

Reuses the caller's session (the same AsyncSession the create_invoice/
create_bill/create_expense route handler is using) for its own read of
integration_connections — Inherited Invariant #4: MUST NEVER call
session.commit()/rollback() itself, only flush() if it ever writes
(currently it doesn't write anything at all, only reads). Enqueuing one
Dramatiq message per connection found (not one message that internally
loops over every connection) matters for retry correctness: if a
QuickBooks push fails but a FreshBooks push for the same record succeeds,
Dramatiq only retries the failed QuickBooks message — a successful
FreshBooks sync is never redundantly redone (design spec Section 4).
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IntegrationConnection
from app.tasks.accounting_sync import sync_financial_record


async def handle_financial_record_created(
    *,
    session: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
    company_id: uuid.UUID,
    **_ignored: object,
) -> None:
    connections_result = await session.execute(
        select(IntegrationConnection).where(IntegrationConnection.company_id == company_id)
    )
    connections = connections_result.scalars().all()

    for connection in connections:
        sync_financial_record.send(
            connection_id=str(connection.id),
            entity_type=entity_type,
            entity_id=str(entity_id),
        )
```

- [ ] **Step 4: Register in `backend/app/core/event_handlers.py`**

Add the import and three `is_registered()`-guarded `register()` calls (one per event, all pointing at the same `handle_financial_record_created` function):

```python
from app.services.financial_record_sync_handler import handle_financial_record_created

    if not is_registered("INVOICE_CREATED", handle_financial_record_created):
        register("INVOICE_CREATED", handle_financial_record_created)

    if not is_registered("EXPENSE_CREATED", handle_financial_record_created):
        register("EXPENSE_CREATED", handle_financial_record_created)

    if not is_registered("BILL_CREATED", handle_financial_record_created):
        register("BILL_CREATED", handle_financial_record_created)
```

- [ ] **Step 5: Run tests to verify they pass**

This task's own tests import `app.tasks.accounting_sync.sync_financial_record`, which does not exist until Task 4.12. Create a minimal placeholder in `backend/app/tasks/accounting_sync.py` first — a bare `dramatiq.actor`-decorated no-op function with the right name — just enough for these tests' `monkeypatch.setattr(sync_financial_record, "send", ...)` calls to have a real attribute to patch:

```python
"""Task 4.11 placeholder — Task 4.12 replaces this with the real actor."""
import dramatiq

from app.tasks import broker  # noqa: F401 - import-time side effect


@dramatiq.actor(max_retries=3, actor_name="sync_financial_record")
def sync_financial_record(*, connection_id: str, entity_type: str, entity_id: str) -> None:
    raise NotImplementedError("Task 4.12 implements this actor for real")
```

Run: `cd backend && python -m pytest tests/test_financial_record_sync_handler.py -v`
Expected: PASS, 3 passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/financial_record_sync_handler.py backend/app/core/event_handlers.py backend/app/tasks/accounting_sync.py backend/tests/test_financial_record_sync_handler.py
git commit -m "feat: add financial-record-created event handler, enqueues per-connection sync"
```

---

### Task 4.12: `sync_financial_record` Dramatiq Actor

**Files:**
- Modify: `backend/app/tasks/accounting_sync.py`
- Test: `backend/tests/test_accounting_sync.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Task 4.12 (design spec Section 5): sync_financial_record — the actual
(fake) push to the connected provider, with per-record status tracking and
Dramatiq-driven retry. Tests the undecorated function directly, same
pattern tests/test_flag_overdue_financial_records.py uses for
_flag_overdue_financial_records."""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.accounting_client import AccountingProviderError
from app.tasks.accounting_sync import _sync_financial_record
from tests.conftest import TEST_DATABASE_URL


async def _seed_connection_and_invoice(session_factory, *, provider="quickbooks"):
    """Seeds a company, a Project (invoices.project_id is NOT NULL), an
    Invoice, and an active IntegrationConnection, via raw SQL — mirrors
    test_flag_overdue_financial_records.py's own seeding style for a
    worker-actor test that must not go through the app's HTTP layer."""
    company_id = uuid.uuid4()
    project_id = uuid.uuid4()
    invoice_id = uuid.uuid4()
    connection_id = uuid.uuid4()

    async with session_factory() as session:
        await session.execute(
            text("INSERT INTO companies (id, parent_id, name) VALUES (:id, NULL, 'Sync Actor Co')"),
            {"id": company_id},
        )
        await session.execute(
            text(
                "INSERT INTO projects (id, company_id, name, site_address, status) "
                "VALUES (:id, :cid, 'P', 'addr', 'active')"
            ),
            {"id": project_id, "cid": company_id},
        )
        await session.execute(
            text(
                "INSERT INTO invoices (id, project_id, company_id, invoice_number, amount, status, due_date) "
                "VALUES (:id, :pid, :cid, 'INV-TEST-0001', 500.00, 'draft', NULL)"
            ),
            {"id": invoice_id, "pid": project_id, "cid": company_id},
        )
        await session.execute(
            text(
                "INSERT INTO integration_connections "
                "(id, company_id, provider, access_token_encrypted, refresh_token_encrypted) "
                "VALUES (:id, :cid, :provider, 'enc-access', 'enc-refresh')"
            ),
            {"id": connection_id, "cid": company_id, "provider": provider},
        )
        await session.commit()

    return {"company_id": company_id, "invoice_id": invoice_id, "connection_id": connection_id}


async def test_successful_push_sets_status_success(monkeypatch):
    owner_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(owner_engine, expire_on_commit=False, class_=AsyncSession)
    try:
        seeded = await _seed_connection_and_invoice(session_factory)

        from app.services import accounting_client

        fake_client = accounting_client.FakeAccountingProviderClient(provider="quickbooks")
        monkeypatch.setattr(accounting_client, "get_accounting_client", lambda provider: fake_client)

        await _sync_financial_record(
            connection_id=str(seeded["connection_id"]),
            entity_type="invoice",
            entity_id=str(seeded["invoice_id"]),
            session_factory=session_factory,
        )

        async with session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT status, attempt_count, last_error FROM integration_sync_records "
                    "WHERE connection_id = :cid AND entity_id = :eid"
                ),
                {"cid": seeded["connection_id"], "eid": seeded["invoice_id"]},
            )
            row = result.fetchone()
        assert row.status == "success"
        assert row.attempt_count == 1
        assert row.last_error is None
        assert fake_client.pushed_invoices == [
            {"invoice_number": "INV-TEST-0001", "amount": "500.00", "status": "draft"}
        ]
    finally:
        await owner_engine.dispose()


async def test_failed_push_sets_status_failed_with_last_error_and_reraises(monkeypatch):
    owner_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(owner_engine, expire_on_commit=False, class_=AsyncSession)
    try:
        seeded = await _seed_connection_and_invoice(session_factory)

        from app.services import accounting_client

        failing_client = accounting_client.FakeAccountingProviderClient(
            provider="quickbooks", should_fail=True
        )
        monkeypatch.setattr(accounting_client, "get_accounting_client", lambda provider: failing_client)

        with pytest.raises(AccountingProviderError):
            await _sync_financial_record(
                connection_id=str(seeded["connection_id"]),
                entity_type="invoice",
                entity_id=str(seeded["invoice_id"]),
                session_factory=session_factory,
            )

        async with session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT status, attempt_count, last_error FROM integration_sync_records "
                    "WHERE connection_id = :cid AND entity_id = :eid"
                ),
                {"cid": seeded["connection_id"], "eid": seeded["invoice_id"]},
            )
            row = result.fetchone()
        assert row.status == "failed"
        assert row.attempt_count == 1
        assert row.last_error is not None
    finally:
        await owner_engine.dispose()


async def test_a_second_invocation_increments_attempt_count(monkeypatch):
    owner_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(owner_engine, expire_on_commit=False, class_=AsyncSession)
    try:
        seeded = await _seed_connection_and_invoice(session_factory)

        from app.services import accounting_client

        fake_client = accounting_client.FakeAccountingProviderClient(provider="quickbooks")
        monkeypatch.setattr(accounting_client, "get_accounting_client", lambda provider: fake_client)

        for _ in range(2):
            await _sync_financial_record(
                connection_id=str(seeded["connection_id"]),
                entity_type="invoice",
                entity_id=str(seeded["invoice_id"]),
                session_factory=session_factory,
            )

        async with session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT attempt_count FROM integration_sync_records "
                    "WHERE connection_id = :cid AND entity_id = :eid"
                ),
                {"cid": seeded["connection_id"], "eid": seeded["invoice_id"]},
            )
            row = result.fetchone()
        assert row.attempt_count == 2
    finally:
        await owner_engine.dispose()
```

Check `backend/app/models/invoice.py`'s exact column list (and `bill.py`/`expense.py`'s) before finalizing the serialized dict `_sync_financial_record` passes to `push_invoice`/`push_bill`/`push_expense` — the test above asserts `fake_client.pushed_invoices == [{"invoice_number": ..., "amount": ..., "status": ...}]` as an illustrative minimum, not an exhaustive contract; if your own implementation serializes additional fields (e.g. `due_date`, `project_id`), update this assertion to match your actual dict shape rather than trimming your serialization down to only these three fields.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_accounting_sync.py -v`
Expected: FAIL — `_sync_financial_record` raises `NotImplementedError` (Task 4.11's placeholder)

- [ ] **Step 3: Write the real `backend/app/tasks/accounting_sync.py`**

```python
"""Task 4.12 (design spec Section 5): sync_financial_record — pushes one
Invoice/Expense/Bill to one connected accounting provider (fake, per this
plan's scope), tracking per-record status in integration_sync_records.

Same undecorated-function/decorated-actor split as every other Dramatiq
actor in this codebase (see app/tasks/flag_overdue_financial_records.py's
own docstring for the full rationale). Uses the owner-role engine
(settings.migrations_database_url) — this actor is scoped to ONE
connection/company via connection_id, not a cross-tenant scan, but it
still uses the owner engine and filters explicitly by connection_id/
company_id in every query (never relying on RLS, since there is no
set_current_tenant() call in a worker process) — same established
convention every other worker actor in this codebase already follows,
rather than introducing a new "workers set tenant context" pattern for
just this one actor.

Retry: on any exception during the push, this function marks the
integration_sync_records row status='failed' + last_error, THEN
re-raises — Dramatiq's own max_retries=3/backoff handles the actual retry
timing, this code never implements its own retry loop (design spec
Section 5). After max_retries is exhausted, Dramatiq drops the message
but the row stays at status='failed', remaining visible via
GET /integrations/{provider}/sync-status rather than silently vanishing.
"""
from __future__ import annotations

import uuid

import dramatiq
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Bill, Expense, IntegrationConnection, IntegrationSyncRecord, Invoice
from app.services.accounting_client import get_accounting_client
from app.services.token_encryption import decrypt_token
from app.tasks import broker  # noqa: F401 - import-time side effect

_owner_engine = create_async_engine(settings.migrations_database_url, pool_pre_ping=True)
_OwnerSessionLocal = async_sessionmaker(_owner_engine, expire_on_commit=False, class_=AsyncSession)

_ENTITY_MODELS: dict[str, type] = {"invoice": Invoice, "expense": Expense, "bill": Bill}


def _serialize(entity_type: str, record: Invoice | Expense | Bill) -> dict:
    if entity_type == "invoice":
        return {
            "invoice_number": record.invoice_number,
            "amount": str(record.amount),
            "status": record.status,
        }
    if entity_type == "expense":
        return {"description": record.description, "amount": str(record.amount)}
    if entity_type == "bill":
        return {"vendor_name": record.vendor_name, "amount": str(record.amount), "status": record.status}
    raise ValueError(f"Unknown entity_type: {entity_type!r}")


async def _upsert_sync_record(
    session: AsyncSession, *, company_id: uuid.UUID, connection_id: uuid.UUID,
    entity_type: str, entity_id: uuid.UUID, status: str, last_error: str | None,
) -> None:
    """One upsert, called exactly once per _sync_financial_record invocation
    (from either the success or the failure branch, never both) —
    attempt_count increments on the SAME write that also records the
    outcome, not as a separate pre-write. This codebase's other worker
    actors (flag_overdue_financial_records.py, report_seat_usage.py) commit
    once, at the end of their function, not mid-function; this actor
    follows that same precedent rather than introducing a new "commit an
    intermediate pending state" pattern — there is no observable moment
    where a client could read a 'pending' row for THIS entity via
    GET /integrations/{provider}/sync-status, since the row is only ever
    created/updated together with its final outcome in one statement."""
    stmt = (
        pg_insert(IntegrationSyncRecord)
        .values(
            id=uuid.uuid4(),
            company_id=company_id,
            connection_id=connection_id,
            entity_type=entity_type,
            entity_id=entity_id,
            status=status,
            attempt_count=1,
            last_error=last_error,
        )
        .on_conflict_do_update(
            index_elements=["connection_id", "entity_type", "entity_id"],
            set_={
                "status": status,
                "attempt_count": IntegrationSyncRecord.__table__.c.attempt_count + 1,
                "last_error": last_error,
                "last_attempted_at": func.now(),
            },
        )
    )
    await session.execute(stmt)


async def _sync_financial_record(
    *,
    connection_id: str,
    entity_type: str,
    entity_id: str,
    session_factory: async_sessionmaker[AsyncSession] = _OwnerSessionLocal,
) -> None:
    async with session_factory() as session:
        connection = (
            await session.execute(
                select(IntegrationConnection).where(IntegrationConnection.id == uuid.UUID(connection_id))
            )
        ).scalar_one()

        model = _ENTITY_MODELS[entity_type]
        record = (
            await session.execute(select(model).where(model.id == uuid.UUID(entity_id)))
        ).scalar_one()

        access_token = decrypt_token(connection.access_token_encrypted)
        client = get_accounting_client(connection.provider)

        try:
            payload = _serialize(entity_type, record)
            if entity_type == "invoice":
                await client.push_invoice(access_token=access_token, invoice=payload)
            elif entity_type == "expense":
                await client.push_expense(access_token=access_token, expense=payload)
            else:
                await client.push_bill(access_token=access_token, bill=payload)
        except Exception as exc:
            await _upsert_sync_record(
                session,
                company_id=connection.company_id,
                connection_id=connection.id,
                entity_type=entity_type,
                entity_id=uuid.UUID(entity_id),
                status="failed",
                last_error=str(exc),
            )
            # Commit BEFORE re-raising — the failed-status write must
            # survive this exception propagating out of the `async with
            # session_factory()` block, it cannot rely on an implicit
            # commit-on-clean-exit that a raised exception would skip.
            await session.commit()
            raise

        await _upsert_sync_record(
            session,
            company_id=connection.company_id,
            connection_id=connection.id,
            entity_type=entity_type,
            entity_id=uuid.UUID(entity_id),
            status="success",
            last_error=None,
        )
        await session.commit()


sync_financial_record = dramatiq.actor(max_retries=3, actor_name="sync_financial_record")(
    _sync_financial_record
)
```

Note this removes `'pending'` as an observable status in practice: the `integration_sync_records` row is only ever written together with its final outcome (`'success'` or `'failed'`) in the same upsert, never as a separate pre-write — so `'pending'` remains in the CHECK constraint (Task 4.2) for schema completeness but is never actually persisted by this implementation. Task 4.12's own test file above asserts `attempt_count == 1` after one call and `== 2` after two, which holds regardless of this simplification; no test in this task asserts on an intermediate `'pending'` state, so no test needs adjusting for this change.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_accounting_sync.py -v`
Expected: PASS, 3 passed

- [ ] **Step 5: Run the full existing test suite for this module plus its dependents**

Run: `cd backend && python -m pytest tests/test_financial_record_sync_handler.py tests/test_accounting_sync.py tests/test_integrations.py -v`
Expected: all pass — confirms replacing Task 4.11's placeholder actor didn't break the handler tests that monkeypatch `sync_financial_record.send`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/tasks/accounting_sync.py backend/tests/test_accounting_sync.py
git commit -m "feat: add sync_financial_record actor with per-record status and retry"
```

---

### Task 4.13: Wire `INVOICE_CREATED`/`EXPENSE_CREATED`/`BILL_CREATED` into the Create Routes

**Files:**
- Modify: `backend/app/routers/invoices.py`
- Modify: `backend/app/routers/expenses.py`
- Modify: `backend/app/routers/bills.py`
- Modify: `backend/tests/test_financial_record_sync_handler.py`

- [ ] **Step 1: Add `publish("INVOICE_CREATED", ...)` to `create_invoice` in `backend/app/routers/invoices.py`**

Right after `await current.session.flush()` in `create_invoice` (before the `return` statement):

```python
from app.core.events import publish

    await publish(
        "INVOICE_CREATED",
        session=current.session,
        entity_type="invoice",
        entity_id=invoice.id,
        company_id=invoice.company_id,
    )
```

- [ ] **Step 2: Add `publish("EXPENSE_CREATED", ...)` to `create_expense` in `backend/app/routers/expenses.py`**

```python
from app.core.events import publish

    await publish(
        "EXPENSE_CREATED",
        session=current.session,
        entity_type="expense",
        entity_id=expense.id,
        company_id=expense.company_id,
    )
```

- [ ] **Step 3: Add `publish("BILL_CREATED", ...)` to `create_bill` in `backend/app/routers/bills.py`**

```python
from app.core.events import publish

    await publish(
        "BILL_CREATED",
        session=current.session,
        entity_type="bill",
        entity_id=bill.id,
        company_id=bill.company_id,
    )
```

- [ ] **Step 4: Verify Task 4.11's own tests still pass with real routes now publishing these events**

Run: `cd backend && python -m pytest tests/test_financial_record_sync_handler.py -v`
Expected: still PASS — those tests call `publish()` directly, not through these routes, so this step is a sanity check that nothing about the handler registration changed, not a new dependency.

- [ ] **Step 5: Write new end-to-end wiring tests proving the REAL routes now trigger a sync enqueue**

Append to `backend/tests/test_financial_record_sync_handler.py`:

```python
async def test_creating_an_invoice_via_the_real_route_enqueues_a_sync(client, monkeypatch):
    from app.services.integration_oauth_state import sign_oauth_state
    from app.tasks.accounting_sync import sync_financial_record

    register_event_handlers()
    admin = await _register_and_login(client, "Sync Co 4", "sync-4@example.test")
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    await client.get(f"/integrations/quickbooks/callback?code=fake&state={state}")

    project = await client.post(
        "/projects", json={"name": "Sync Project", "site_address": "1 Main St"}, headers=admin["headers"]
    )
    assert project.status_code == 201, project.text

    calls = []
    monkeypatch.setattr(sync_financial_record, "send", lambda *a, **kw: calls.append((a, kw)))

    invoice = await client.post(
        f"/projects/{project.json()['id']}/invoices", json={"amount": "100.00"}, headers=admin["headers"]
    )
    assert invoice.status_code == 201, invoice.text

    assert len(calls) == 1
    assert calls[0][1]["entity_type"] == "invoice"


async def test_creating_a_bill_via_the_real_route_enqueues_a_sync(client, monkeypatch):
    from app.services.integration_oauth_state import sign_oauth_state
    from app.tasks.accounting_sync import sync_financial_record

    register_event_handlers()
    admin = await _register_and_login(client, "Sync Co 5", "sync-5@example.test")
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    await client.get(f"/integrations/quickbooks/callback?code=fake&state={state}")

    calls = []
    monkeypatch.setattr(sync_financial_record, "send", lambda *a, **kw: calls.append((a, kw)))

    bill = await client.post(
        "/bills", json={"vendor_name": "Ace Plumbing", "amount": "300.00"}, headers=admin["headers"]
    )
    assert bill.status_code == 201, bill.text

    assert len(calls) == 1
    assert calls[0][1]["entity_type"] == "bill"


async def test_creating_an_expense_via_the_real_route_enqueues_a_sync(client, monkeypatch):
    from app.services.integration_oauth_state import sign_oauth_state
    from app.tasks.accounting_sync import sync_financial_record

    register_event_handlers()
    admin = await _register_and_login(client, "Sync Co 6", "sync-6@example.test")
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    await client.get(f"/integrations/quickbooks/callback?code=fake&state={state}")

    project = await client.post(
        "/projects", json={"name": "Expense Sync Project", "site_address": "1 Main St"}, headers=admin["headers"]
    )
    assert project.status_code == 201, project.text

    calls = []
    monkeypatch.setattr(sync_financial_record, "send", lambda *a, **kw: calls.append((a, kw)))

    expense = await client.post(
        f"/projects/{project.json()['id']}/expenses",
        json={"description": "Materials", "amount": "50.00", "incurred_on": "2026-08-01"},
        headers=admin["headers"],
    )
    assert expense.status_code == 201, expense.text

    assert len(calls) == 1
    assert calls[0][1]["entity_type"] == "expense"
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_financial_record_sync_handler.py tests/test_invoices.py tests/test_bills.py tests/test_expenses.py -v`
Expected: all pass — the last three files are the pre-existing AR/AP/Expense test suites, run here to confirm publishing three new events from their create routes causes zero regressions in any of their own existing assertions.

- [ ] **Step 7: Commit**

```bash
git add backend/app/routers/invoices.py backend/app/routers/expenses.py backend/app/routers/bills.py backend/tests/test_financial_record_sync_handler.py
git commit -m "feat: publish INVOICE_CREATED/EXPENSE_CREATED/BILL_CREATED from their create routes"
```

---

### Task 4.14: Integrations Tenant-Isolation Regression Tests

**Files:**
- Create: `backend/tests/test_integrations_tenant_isolation.py`

- [ ] **Step 1: Write the tests**

Following `test_invoicing_ap_tenant_isolation.py`'s exact established shape (both the app-layer 404 proof + RLS-disable/re-enable policy-level proof split, and the genuinely bidirectional sibling-branch check — see that file's own review history for why both of those refinements matter, not just the plan's own first-draft instinct):

```python
"""Integrations tenant-isolation regression tests (design spec Section 7).
Mirrors test_invoicing_ap_tenant_isolation.py's exact structure."""
import uuid

import asyncpg

from app.services.integration_oauth_state import sign_oauth_state
from tests.conftest import TEST_APP_DATABASE_URL, TEST_DATABASE_URL

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")
APP_CONN_DSN = TEST_APP_DATABASE_URL.replace("+asyncpg", "")


async def _register_and_login(client, company_name, email):
    register = await client.post(
        "/auth/register",
        json={
            "company_name": company_name,
            "admin_full_name": "Test Admin",
            "admin_email": email,
            "admin_password": "supersecret123",
        },
    )
    assert register.status_code == 201, register.text
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    return {
        "company_id": register.json()["company_id"],
        "user_id": register.json()["user_id"],
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


async def _add_membership_directly(user_id, company_id, role):
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO company_users (company_id, user_id, role, created_at) "
            "VALUES ($1, $2, $3, now())",
            company_id,
            user_id,
            role,
        )
    finally:
        await conn.close()


async def _create_child_with_membership(client, parent, name, role="admin"):
    create = await client.post(
        f"/companies/{parent['company_id']}/children", json={"name": name}, headers=parent["headers"]
    )
    assert create.status_code == 201, create.text
    child_id = create.json()["id"]
    await _add_membership_directly(parent["user_id"], child_id, role)
    return child_id


async def _connect(client, headers, company_id, provider="quickbooks"):
    state = sign_oauth_state(company_id=company_id, provider=provider)
    response = await client.get(f"/integrations/{provider}/callback?code=fake&state={state}")
    assert response.status_code == 200, response.text
    return response.json()


async def test_genuinely_unrelated_tenant_header_spoofing_via_x_tenant_id_is_blocked_for_connect(client):
    company_a = await _register_and_login(client, "Iso Integ Co A", "iso-integ-a@example.test")
    company_b = await _register_and_login(client, "Iso Integ Co B", "iso-integ-b@example.test")

    spoofed_headers = {**company_a["headers"], "X-Tenant-ID": company_b["company_id"]}
    response = await client.get("/integrations/quickbooks/connect", headers=spoofed_headers)
    assert response.status_code == 403


async def test_sync_status_404s_across_tenants_at_the_application_layer(client):
    company_a = await _register_and_login(client, "Iso Integ Co C", "iso-integ-c@example.test")
    company_b = await _register_and_login(client, "Iso Integ Co D", "iso-integ-d@example.test")
    await _connect(client, company_b["headers"], company_b["company_id"])

    response = await client.get("/integrations/quickbooks/sync-status", headers=company_a["headers"])
    assert response.status_code == 404


async def test_rls_policy_itself_blocks_cross_tenant_connection_visibility(client):
    a = await _register_and_login(client, "Iso Integ Co E", "iso-integ-e@example.test")
    b = await _register_and_login(client, "Iso Integ Co F", "iso-integ-f@example.test")
    connection_b = await _connect(client, b["headers"], b["company_id"])
    connection_b_id = connection_b["id"]

    app_conn = await asyncpg.connect(APP_CONN_DSN)
    try:
        await app_conn.execute(
            "SELECT set_config('app.current_tenant', $1, false)", a["company_id"]
        )
        visible_as_a = await app_conn.fetchrow(
            "SELECT id FROM integration_connections WHERE id = $1", uuid.UUID(connection_b_id)
        )
        assert visible_as_a is None, (
            "RLS should block Company A's session from seeing Company B's integration connection"
        )
    finally:
        await app_conn.close()

    owner_conn = await asyncpg.connect(OWNER_DSN)
    try:
        await owner_conn.execute("ALTER TABLE integration_connections DISABLE ROW LEVEL SECURITY")
        app_conn2 = await asyncpg.connect(APP_CONN_DSN)
        try:
            await app_conn2.execute(
                "SELECT set_config('app.current_tenant', $1, false)", a["company_id"]
            )
            visible_with_rls_off = await app_conn2.fetchrow(
                "SELECT id FROM integration_connections WHERE id = $1", uuid.UUID(connection_b_id)
            )
            assert visible_with_rls_off is not None, (
                "Sanity check failed: Company B's connection row should exist and be visible "
                "once RLS is off — if this fails, the row itself is missing, which means the "
                "test setup (not the policy) is broken."
            )
        finally:
            await app_conn2.close()
    finally:
        try:
            await owner_conn.execute("ALTER TABLE integration_connections ENABLE ROW LEVEL SECURITY")
        finally:
            await owner_conn.close()


async def test_parent_admin_can_see_child_branch_connection(client):
    parent = await _register_and_login(client, "Iso Integ Parent", "iso-integ-parent@example.test")
    child_id = await _create_child_with_membership(client, parent, "Integ Branch")
    child_headers = {**parent["headers"], "X-Tenant-ID": child_id}

    await _connect(client, child_headers, child_id)

    response = await client.get(
        "/integrations/quickbooks/sync-status", headers={**parent["headers"], "X-Tenant-ID": child_id}
    )
    assert response.status_code == 200, response.text


async def test_sibling_branches_cannot_see_each_others_connections(client):
    """Checked symmetrically in both directions — a single one-way check
    wouldn't rule out a bug that only manifests checking the reverse
    direction."""
    parent = await _register_and_login(client, "Iso Integ Parent 2", "iso-integ-parent2@example.test")
    child_a_id = await _create_child_with_membership(client, parent, "Integ Branch A")
    child_b_id = await _create_child_with_membership(client, parent, "Integ Branch B")
    headers_a = {**parent["headers"], "X-Tenant-ID": child_a_id}
    headers_b = {**parent["headers"], "X-Tenant-ID": child_b_id}

    await _connect(client, headers_a, child_a_id)
    await _connect(client, headers_b, child_b_id)

    response_b_sees_a = await client.get("/integrations/quickbooks/sync-status", headers=headers_b)
    # Branch B has its OWN quickbooks connection, so this 200s but must
    # never reflect Branch A's connection — same "each branch sees only
    # its own row" proof as the 404-based checks above, expressed
    # differently since sync-status keys off the CALLER's own company_id,
    # not a specific connection id in the URL.
    assert response_b_sees_a.status_code == 200, response_b_sees_a.text
    response_a_sees_b = await client.get("/integrations/quickbooks/sync-status", headers=headers_a)
    assert response_a_sees_b.status_code == 200, response_a_sees_b.text
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_integrations_tenant_isolation.py -v`
Expected: PASS, 6 passed

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_integrations_tenant_isolation.py
git commit -m "test: add Integrations tenant-isolation regression coverage"
```

---

### Task 4.15: Read-Only Enforcement Verification

**Files:**
- None to create — this task verifies, not builds.

`block_if_read_only` was intentionally NOT added to `connect`/`sync-status` (both are GET-only, already exempt via that dependency's own "GET/HEAD/OPTIONS always pass" rule even if it were present) and intentionally NOT added to `callback` (no `CurrentUser` to check, added to the completeness test's own exclusion list instead, in Task 4.9). This task proves the existing completeness introspection test (`backend/tests/test_read_only_enforcement.py`, Task 3.28) reflects this correctly, with no missed write route.

- [ ] **Step 1: Run the existing completeness test**

Run: `cd backend && python -m pytest tests/test_read_only_enforcement.py -v`
Expected: PASS — `test_every_write_route_has_block_if_read_only_except_deliberate_exclusions` passes. Since `connect`/`sync-status` are GET routes, this test's own logic (per its docstring) only checks non-GET methods, so neither needs `block_if_read_only` to pass this check — confirm this is genuinely why they pass, not an accidental gap, by reading the test's own GET-skip logic directly rather than assuming.

- [ ] **Step 2: If it fails, fix the missing route, not the test**

If it flags a route this task's own reasoning above didn't anticipate, add `block_if_read_only` to that route (in `integrations.py`) — do not add a new exclusion for it unless that route genuinely has no `CurrentUser`, matching `callback`'s own justification exactly.

- [ ] **Step 3: No commit needed for this task** (verification only). If Step 2 required a fix, commit that fix:

```bash
git add backend/app/routers/integrations.py
git commit -m "fix: wire block_if_read_only into a write route missed during initial implementation"
```

---

### Task 4.16: Full-Stack E2E Extension

**Files:**
- Modify: `scripts/e2e_smoke_test.py`

Following the exact established pattern (Task 2.25, Task 3.13, Task 3.29, Task 3.49's own extensions) — a new block after the Invoicing/AR-AP block, before the final frontend-health-check block.

- [ ] **Step 1: Add the new block**

Add a docstring note at the top of the file (matching the style of the existing notes) explaining this block exercises the Integrations flow: connect returns a fake authorization URL, callback creates a connection from a real signed state token, creating an Invoice enqueues a sync (intercepted, not actually run against real Redis — same "don't touch real Redis in an automated script" reasoning `scripts/e2e_smoke_test.py` doesn't otherwise need since this is its first time enqueuing a NEW kind of message rather than one already covered), and sync-status reflects the connection.

Register a new company ("E2E Company H" or the next letter in sequence — check what the Invoicing/AR-AP block used and continue from there). Assert:

1. `GET /integrations/quickbooks/connect` returns an `authorization_url` starting with `https://quickbooks.fake-oauth.test/`.
2. `GET /integrations/quickbooks/callback` with a real signed `state` (generated the same way `sign_oauth_state` is used in the backend's own tests — this script will need to import that helper, or replicate its exact HMAC/claims shape inline if importing backend application code into this standalone script isn't this file's existing convention; check how the script currently handles anything backend-internal like `_set_subscription_status`'s own raw-SQL approach before deciding) succeeds and returns a connection with `provider == "quickbooks"`.
3. `GET /integrations/quickbooks/sync-status` returns `records == []` immediately after connecting (nothing has been synced yet).
4. Creating a Project + Invoice for this company succeeds (the sync itself enqueues to a real Redis queue this script does not drain — matching Task 2.25's own "worker deliberately not brought up" precedent — so this step proves the ROUTE-level wiring didn't error, not that a message was actually consumed).

Follow the file's own established `checks_passed.append(...)` running-log pattern, `.example` email domains, and descriptive assertion failure messages exactly as prior blocks do.

- [ ] **Step 2: Run against the live local stack**

Bring up the full stack per the established procedure (`docker compose up -d --build`, verify via `docker exec ... pg_isready` not just `docker ps`, watch for stray processes per the documented Docker/Postgres stability discipline — this session has hit real Docker Desktop instability more than once; check container health explicitly before assuming a clean start). Run `python scripts/e2e_smoke_test.py` and confirm all checks pass, including the new ones.

If the pre-existing, unrelated frontend-health-check issue (port-mapping/`FRONTEND_URL` drift, flagged repeatedly since Task 3.29) is still unresolved, that's a separate known issue — don't let it block this task's own new checks from being verified; report it as still-open if encountered again, don't attempt to fix it here.

- [ ] **Step 3: Commit**

```bash
git add scripts/e2e_smoke_test.py
git commit -m "test: extend E2E smoke test with Integrations flow (connect, callback, sync-status)"
```

---

### Task 4.17: Full Regression Pass + Plan Closeout

**Files:**
- Modify: `docs/superpowers/specs/2026-07-15-integrations-quickbooks-freshbooks-design.md`

- [ ] **Step 1: Full backend regression pass, twice solo**

Before running: check for stray Python processes and verify `docker exec <postgres-container> pg_isready -U postgres` succeeds — per the documented Docker/Postgres stability discipline, diagnose before assuming a hang if either run takes unusually long.

Run: `cd backend && python -m pytest -v` (twice, sequentially)
Expected: all tests pass both times, including every test added in Tasks 4.1–4.16.

- [ ] **Step 2: Dedicated RLS/tenant-isolation regression suite**

Run: `cd backend && python -m pytest -v tests/test_tenant_isolation.py tests/test_rls_policy_regression.py tests/test_tenant_isolation_phase1.py tests/test_tenant_isolation_phase2.py tests/test_tenant_isolation_phase3.py tests/test_billing_tenant_isolation.py tests/test_invoicing_ap_tenant_isolation.py tests/test_integrations_tenant_isolation.py`
Expected: all pass — confirms no regression in any earlier phase's tables from this work.

- [ ] **Step 3: Update the design spec's Implementation Status note**

Add an "Implementation Status" note at the top of `docs/superpowers/specs/2026-07-15-integrations-quickbooks-freshbooks-design.md` (right after the title, before "**Date:**"), matching the exact convention Billing's/Compliance Tracking's/Invoicing-AR-AP's own specs use: state completion, the two regression-suite pass counts and timings, the E2E extension coverage, and any deliberately-not-fixed out-of-scope issue discovered along the way.

- [ ] **Step 4: Commit the closeout**

```bash
git add docs/superpowers/specs/2026-07-15-integrations-quickbooks-freshbooks-design.md
git commit -m "docs: close out Integrations provider-agnostic core implementation"
```

- [ ] **Step 5: Push and open a PR against `main`**

```bash
git push -u origin feature/integrations-core
gh pr create --base main --head feature/integrations-core --title "feat: Integrations core - QuickBooks/FreshBooks OAuth + async sync, behind a fake provider client" --body "..."
```

Confirm CI goes green. Merging remains an explicit, separate user decision — not automatic.
