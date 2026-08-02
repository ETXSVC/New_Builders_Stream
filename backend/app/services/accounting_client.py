"""Task 4.4 (design spec Section 2): the AccountingProviderClient
abstraction. Every consumer in this codebase (the connect/callback routes,
the sync actor) depends only on the AccountingProviderClient Protocol
below, never on a real QuickBooks/FreshBooks SDK directly — same
"swap the real thing for a fake behind an interface" pattern
app/services/stripe_client.py already establishes for Stripe, applied here
to a second class of external paid API.

FakeAccountingProviderClient is the default and the only implementation the
whole test suite exercises. `RealQuickBooksClient`/`RealFreshBooksClient`
(app/services/quickbooks_client.py, app/services/freshbooks_client.py) are
selected per provider by configuration — see `get_accounting_client`.

Typed methods per operation (push_invoice/push_expense/push_bill), not one
generic push_record(entity_type, ...) dispatch method — matching
StripeClient's own established shape, since a real SDK genuinely has
separate endpoints per record type.

`idempotency_key` on every push_* method (added alongside Task 4.12's own
retry-safety fix, `app/tasks/accounting_sync.py`): the sync actor commits
its own "this record synced successfully" bookkeeping AFTER the push call
returns, so a failure in that bookkeeping step (not the push itself) makes
Dramatiq retry the whole actor — which would otherwise call push_* a
second time for a push that already succeeded. The sync actor always passes
the entity_id (stable across retries of the SAME logical sync) as this key.
QuickBooks implements exactly this as a `requestid` query parameter, which
replays the original response instead of creating a second transaction.

## What the fake never needed, and a real provider does

Three things, each of which changed this Protocol rather than living inside
an implementation — the integrations spec's own Open Questions predicted
the first two and inherited them as requirements:

* **An account id.** Neither provider's access token addresses anything by
  itself. QuickBooks hands back a `realmId` as a query parameter on the
  OAuth *callback* (not in the token response); FreshBooks makes you fetch
  an `accountId` from `/auth/api/v1/users/me` afterwards. Both are a path
  segment of every request. `AccountingTokens.account_id` carries it, and
  `exchange_code_for_tokens` takes the callback's value so the QuickBooks
  client can pass it through and the FreshBooks client can ignore it and go
  fetch its own.

* **Token refresh.** QuickBooks access tokens last 1 hour, FreshBooks 12,
  and BOTH providers rotate the refresh token on every use — the old one
  stops working the moment a new pair is issued. So `refresh_access_token`
  returns a full new pair that the caller must persist, and a caller that
  refreshes without saving the result has permanently broken that
  connection. `AccountingAuthError` exists to tell that caller when to do
  it: it is raised only for an authentication failure, so the sync actor
  can refresh and retry once on exactly that and not on a validation error
  that would fail identically the second time.

* **Entity resolution.** A QuickBooks invoice needs a `CustomerRef` naming a
  real Customer in the tenant's own books; a bill needs a `VendorRef`; both
  need line items pointing at real Items/Accounts. Builders Stream has none
  of these — `invoices` has no client column at all. `resolve_entity` maps
  a local display name onto a provider id, creating it if absent, and the
  sync actor caches the answer in `integration_entity_mappings` (migration
  0030). Caching is not an optimization: a find-or-create that forgets
  eventually creates a second "Acme Holdings" in somebody's real accounting
  file.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from typing import Protocol

from app.config import settings


class AccountingProviderError(Exception):
    """Raised by a push_* call when the provider rejects or fails to process
    a record. The caller (the sync actor, Task 4.12) is responsible for
    recording this as a failed sync attempt and re-raising so Dramatiq's own
    retry picks it back up — not this module's job."""


class AccountingAuthError(AccountingProviderError):
    """The provider rejected the ACCESS TOKEN specifically (401/403).

    A subclass, so every existing `except AccountingProviderError` still
    catches it and the failure path is unchanged. It exists so the sync
    actor can tell the one error worth retrying differently — refresh the
    token and try once more — from every other error, where a second
    attempt with the same payload fails the same way. Guessing this from a
    message string would be the alternative, and would silently start
    refreshing on unrelated errors the day a provider reworded one.
    """


class AccountingConfigurationError(Exception):
    """Raised at construction when a real client cannot possibly work.

    Same shape and same reason as stripe_client.StripeConfigurationError:
    surfaced when the client is built rather than at the first push, so a
    missing client secret is a visible failure rather than one tenant's
    sync quietly failing every night.
    """


@dataclass
class AccountingTokens:
    """One provider's answer to "who are you and what may you touch".

    `account_id` is nullable only because the fake has no real notion of
    one; both real clients always populate it, and the sync actor treats a
    missing value on a real connection as "reconnect this provider".
    """

    access_token: str
    refresh_token: str
    account_id: str | None = None


@dataclass(frozen=True)
class RefSpec:
    """One provider id a record cannot be created without.

    The two providers genuinely disagree about what they need: a QuickBooks
    invoice line requires an `ItemRef` pointing at a real Item, while a
    FreshBooks invoice line is free text and needs no such thing;
    QuickBooks wants accounts by type, FreshBooks wants an expense category.
    Hardcoding QuickBooks' answer into the sync actor would make the actor
    wrong for FreshBooks and wrong again for the next provider.

    So each client DECLARES its requirements (`required_refs`) and the actor
    satisfies them generically: resolve, cache, and hand back the ids under
    `payload_key`. This is a pure description with no I/O, which is what
    lets the actor decide what to look up before it talks to anyone.

    `from_payload` names a field on the record dict holding the local name
    to match on (a client's name, a bill's vendor). `literal` is for refs
    that are the same every time — an account role, or the single service
    item every invoice line books against.
    """

    payload_key: str
    kind: str
    from_payload: str | None = None
    literal: str | None = None

    def local_key(self, record: dict) -> str | None:
        if self.literal is not None:
            return self.literal
        if self.from_payload is None:  # pragma: no cover - construction error
            return None
        value = record.get(self.from_payload)
        return str(value) if value else None


class AccountingProviderClient(Protocol):
    def required_refs(self, record_type: str) -> tuple[RefSpec, ...]: ...

    async def get_authorization_url(self, *, company_id: str, state: str) -> str: ...

    async def exchange_code_for_tokens(
        self, *, code: str, callback_account_id: str | None = None
    ) -> AccountingTokens: ...

    async def refresh_access_token(self, *, refresh_token: str) -> AccountingTokens: ...

    async def resolve_entity(
        self, *, access_token: str, account_id: str, kind: str, name: str
    ) -> str: ...

    async def push_invoice(
        self, *, access_token: str, account_id: str, invoice: dict, idempotency_key: str
    ) -> str: ...

    async def push_expense(
        self, *, access_token: str, account_id: str, expense: dict, idempotency_key: str
    ) -> str: ...

    async def push_bill(
        self, *, access_token: str, account_id: str, bill: dict, idempotency_key: str
    ) -> str: ...

    async def push_payment(
        self, *, access_token: str, account_id: str, payment: dict, idempotency_key: str
    ) -> str: ...


@dataclass
class FakeAccountingProviderClient:
    """In-memory, deterministic, no network calls. `provider` only shapes
    cosmetic details (the fake authorization URL's hostname, fake
    external-ID prefixes) — it never branches behavior, since there is
    nothing real to branch between here. `pushed_invoices`/`pushed_expenses`/
    `pushed_bills` accumulate every successful push as the raw dict passed
    in, for tests to assert against — same accumulator pattern
    FakeStripeClient's own `reported_usage` already establishes.
    `should_fail`, when True, makes every push_* call raise
    AccountingProviderError instead of accumulating — the one test hook
    needed to exercise the retry/failure path without real network
    flakiness. `should_fail_auth` does the same with AccountingAuthError,
    which is the hook for the refresh-and-retry path.

    `_synced_keys` is the fake provider-side idempotency dedup store (see
    this module's own docstring): a `(operation, idempotency_key) ->
    external_id` map, populated only on a successful push. A repeat
    push_* call with a key already in this map returns the SAME external_id
    immediately — no `should_fail` check, no re-append to `pushed_*` — the
    same "already processed this exact operation, here's what happened
    last time" contract a real provider's idempotency handling gives.
    Keyed on `(operation, idempotency_key)`, not `idempotency_key` alone,
    since the same entity_id is never reused across entity types but this
    keeps the map's intent explicit rather than relying on that as an
    unstated invariant."""

    provider: str
    should_fail: bool = False
    # Raises AccountingAuthError instead of AccountingProviderError. Counts
    # down rather than latching, so a test can say "fail auth once, then
    # succeed" — which is the actual shape of an expired access token being
    # refreshed, and the only way to prove the retry runs exactly once.
    fail_auth_times: int = 0
    pushed_invoices: list[dict] = field(default_factory=list)
    pushed_expenses: list[dict] = field(default_factory=list)
    pushed_bills: list[dict] = field(default_factory=list)
    pushed_payments: list[dict] = field(default_factory=list)
    resolved_entities: list[tuple[str, str]] = field(default_factory=list)
    refresh_calls: int = 0
    _synced_keys: dict[tuple[str, str], str] = field(default_factory=dict)

    def required_refs(self, record_type: str) -> tuple[RefSpec, ...]:
        # None. The fake invents its own ids and has no chart of accounts to
        # reference, so it needs nothing resolved — which also keeps every
        # existing test's payload assertions unchanged.
        return ()

    async def get_authorization_url(self, *, company_id: str, state: str) -> str:
        return f"https://{self.provider}.fake-oauth.test/authorize?state={state}"

    async def exchange_code_for_tokens(
        self, *, code: str, callback_account_id: str | None = None
    ) -> AccountingTokens:
        return AccountingTokens(
            access_token=f"access_fake_{uuid.uuid4().hex[:16]}",
            refresh_token=f"refresh_fake_{uuid.uuid4().hex[:16]}",
            # Echoes the callback's value when there is one, so a test can
            # assert the route actually forwarded it.
            account_id=callback_account_id or f"acct_fake_{uuid.uuid4().hex[:8]}",
        )

    async def refresh_access_token(self, *, refresh_token: str) -> AccountingTokens:
        self.refresh_calls += 1
        # A NEW refresh token, not the one passed in — both real providers
        # rotate, and a fake that returned the same value would let a caller
        # that forgets to persist the new pair pass its tests and then
        # permanently break real connections.
        return AccountingTokens(
            access_token=f"access_fake_{uuid.uuid4().hex[:16]}",
            refresh_token=f"refresh_fake_{uuid.uuid4().hex[:16]}",
        )

    async def resolve_entity(
        self, *, access_token: str, account_id: str, kind: str, name: str
    ) -> str:
        self.resolved_entities.append((kind, name))
        return f"{self.provider}_{kind}_{abs(hash(name)) % 10_000}"

    def _check_failure(self, operation: str) -> None:
        if self.fail_auth_times > 0:
            self.fail_auth_times -= 1
            raise AccountingAuthError(f"Fake {self.provider} {operation} auth failure")
        if self.should_fail:
            raise AccountingProviderError(f"Fake {self.provider} {operation} failure")

    def _push(self, operation: str, record: dict, accumulator: list[dict], key: str) -> str:
        cache_key = (operation, key)
        if cache_key in self._synced_keys:
            return self._synced_keys[cache_key]
        self._check_failure(f"push_{operation}")
        accumulator.append(record)
        external_id = f"{self.provider}_{operation[:4]}_{uuid.uuid4().hex[:16]}"
        self._synced_keys[cache_key] = external_id
        return external_id

    async def push_invoice(
        self, *, access_token: str, account_id: str, invoice: dict, idempotency_key: str
    ) -> str:
        return self._push("invoice", invoice, self.pushed_invoices, idempotency_key)

    async def push_expense(
        self, *, access_token: str, account_id: str, expense: dict, idempotency_key: str
    ) -> str:
        return self._push("expense", expense, self.pushed_expenses, idempotency_key)

    async def push_bill(
        self, *, access_token: str, account_id: str, bill: dict, idempotency_key: str
    ) -> str:
        return self._push("bill", bill, self.pushed_bills, idempotency_key)

    async def push_payment(
        self, *, access_token: str, account_id: str, payment: dict, idempotency_key: str
    ) -> str:
        return self._push("payment", payment, self.pushed_payments, idempotency_key)


def get_accounting_client(provider: str) -> AccountingProviderClient:
    """The one seam the real implementations plug into (design spec
    Section 2).

    Per-provider, and gated on that provider's own client id being
    configured — a tenant can have real QuickBooks while FreshBooks is
    still the fake, because the two are separate OAuth applications with
    separate approval processes and there is no reason to make one wait for
    the other.

    Imports are function-local so a deployment running the fake never
    imports the real clients, and so this module stays importable in the
    same way `stripe_client` does.
    """
    if provider == "quickbooks" and settings.quickbooks_client_id:
        from app.services.quickbooks_client import RealQuickBooksClient

        return RealQuickBooksClient(
            client_id=settings.quickbooks_client_id,
            client_secret=settings.quickbooks_client_secret or "",
            redirect_uri=_redirect_uri("quickbooks"),
            environment=settings.quickbooks_environment,
        )

    if provider == "freshbooks" and settings.freshbooks_client_id:
        from app.services.freshbooks_client import RealFreshBooksClient

        return RealFreshBooksClient(
            client_id=settings.freshbooks_client_id,
            client_secret=settings.freshbooks_client_secret or "",
            redirect_uri=_redirect_uri("freshbooks"),
        )

    return FakeAccountingProviderClient(provider=provider)


def _redirect_uri(provider: str) -> str:
    """The callback URL registered with the provider's OAuth app.

    Derived from `frontend_base_url` rather than configured separately
    because it has to match `GET /integrations/{provider}/callback` exactly
    — a provider rejects the exchange outright on any mismatch, and two
    settings that must agree are a way to have them disagree. Overridable
    for the case this cannot cover: a deployment whose backend is reached on
    a different hostname than its frontend.
    """
    if settings.integrations_redirect_base_url:
        base = settings.integrations_redirect_base_url
    else:
        base = settings.frontend_base_url
    return f"{base.rstrip('/')}/integrations/{provider}/callback"
