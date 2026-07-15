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
