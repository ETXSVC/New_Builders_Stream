"""Task 3.17: the StripeClient abstraction (design spec Section 2).

Every consumer in this codebase (registration's trial creation, the
subscriptions router, the seat-usage job, the webhook handler) depends only
on the StripeClient Protocol below, never on the real `stripe` SDK
directly — same "swap the real thing for a fake behind an interface"
pattern app/services/document_storage.py already establishes for local
filesystem vs. a future S3 backend, applied here to an external paid API
instead.

FakeStripeClient is the ONLY implementation this plan builds. It is
deterministic and in-memory: no network calls, no real Stripe account
needed to build or test this entire feature. A RealStripeClient wrapping
the actual `stripe` SDK against live test-mode credentials is explicit
future work (design spec Scope section) — when it's built, it implements
this same Protocol; nothing in this file or its consumers changes.

Webhook signature scheme: real Stripe signs webhook payloads with HMAC-SHA256
over the raw request body using a per-endpoint shared secret (the
`Stripe-Signature` header in production is actually
`t=<timestamp>,v1=<hex-hmac>`, but this fake simplifies to a bare hex HMAC
digest — the real implementation's job is matching Stripe's exact header
format; this fake's job is proving the verify/reject logic this codebase's
own handler depends on, which doesn't require reproducing Stripe's full
header syntax).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol


class StripeSignatureError(Exception):
    """Raised when a webhook payload's signature does not match — the
    caller (the webhook route, Task 3.21) is responsible for turning this
    into an HTTP 400, not this module."""


@dataclass
class StripeSubscription:
    stripe_subscription_id: str
    status: str
    current_period_end: datetime


class StripeClient(Protocol):
    async def create_customer(self, *, email: str, name: str) -> str: ...

    async def create_trialing_subscription(
        self, *, customer_id: str, tier: str, trial_days: int
    ) -> StripeSubscription: ...

    async def create_portal_session(self, *, customer_id: str) -> str: ...

    async def report_seat_usage(self, *, stripe_subscription_id: str, quantity: int) -> None: ...

    def verify_webhook_signature(self, *, payload: bytes, signature_header: str) -> dict[str, Any]: ...


@dataclass
class FakeStripeClient:
    """In-memory, deterministic, no network calls. `reported_usage`
    accumulates every `report_seat_usage` call as `(stripe_subscription_id,
    quantity)` tuples, for tests to assert against."""

    webhook_secret: str = "fake_webhook_secret_for_tests"
    reported_usage: list[tuple[str, int]] = field(default_factory=list)

    async def create_customer(self, *, email: str, name: str) -> str:
        return f"cus_fake_{uuid.uuid4().hex[:16]}"

    async def create_trialing_subscription(
        self, *, customer_id: str, tier: str, trial_days: int
    ) -> StripeSubscription:
        return StripeSubscription(
            stripe_subscription_id=f"sub_fake_{uuid.uuid4().hex[:16]}",
            status="trialing",
            current_period_end=datetime.now(timezone.utc) + timedelta(days=trial_days),
        )

    async def create_portal_session(self, *, customer_id: str) -> str:
        return f"https://billing.stripe.test/portal/{uuid.uuid4().hex[:16]}"

    async def report_seat_usage(self, *, stripe_subscription_id: str, quantity: int) -> None:
        self.reported_usage.append((stripe_subscription_id, quantity))

    def verify_webhook_signature(self, *, payload: bytes, signature_header: str) -> dict[str, Any]:
        expected = hmac.new(self.webhook_secret.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature_header):
            raise StripeSignatureError("Webhook signature does not match")

        return json.loads(payload)
