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

Webhook signature scheme: Stripe signs webhook payloads with HMAC-SHA256
over `"{timestamp}.{raw_body}"` and sends `Stripe-Signature:
t=<unix-ts>,v1=<hex-hmac>` (possibly with several `v1=` values during a
secret rotation, and other `vN=` schemes that must be ignored).

This module implements **that real format**, not a simplified one, and the
verification lives in `verify_stripe_signature_header` — a plain function
shared by the fake and by any future SDK-backed client, because the header
format is Stripe's, not an implementation's. Two properties matter and
neither is optional:

  * **the timestamp is inside the signed string**, so it can't be edited
    without breaking the HMAC; and
  * **a replay window** (`STRIPE_SIGNATURE_TOLERANCE_SECONDS`) is enforced
    against it.

Without the window, a captured-and-replayed body stays valid forever — and
since `POST /webhooks/stripe` is public and unauthenticated apart from this
signature, that is the whole security boundary on a route that can move any
tenant's subscription to `active`. An earlier version of this file signed a
bare hex digest with no timestamp at all, which made replay protection
impossible to express.
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
    """Raised when a webhook payload's signature does not match, is
    malformed, or is outside the replay window — the caller (the webhook
    route, Task 3.21) is responsible for turning this into an HTTP 400, not
    this module.

    Deliberately one exception for all three: the route returns the same
    400 either way, and distinguishing "bad signature" from "too old" for
    an anonymous caller only tells an attacker which half of the check to
    work on.
    """


# Stripe's own default tolerance is 5 minutes, and matching it keeps the
# window as small as legitimate delivery allows. Wider would extend how long
# a captured request stays replayable; much narrower would start rejecting
# genuine deliveries on ordinary clock skew between Stripe and this host.
STRIPE_SIGNATURE_TOLERANCE_SECONDS = 300


def build_stripe_signature_header(*, payload: bytes, secret: str, timestamp: int) -> str:
    """Produce a `t=...,v1=...` header exactly as Stripe would.

    Lives here rather than in the tests because the fake client needs it to
    hand out valid headers, and because a single implementation means the
    signer and the verifier cannot drift into agreeing with each other on a
    format Stripe doesn't actually use.
    """
    signed_payload = f"{timestamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_stripe_signature_header(
    *,
    payload: bytes,
    signature_header: str,
    secret: str,
    tolerance_seconds: int = STRIPE_SIGNATURE_TOLERANCE_SECONDS,
    now: int | None = None,
) -> dict[str, Any]:
    """Verify a real `Stripe-Signature` header and return the parsed event.

    Raises `StripeSignatureError` on a malformed header, an unparseable or
    out-of-window timestamp, or no matching `v1` signature.

    `now` is injectable purely so tests can pin the clock; production always
    uses the real one.
    """
    parts: dict[str, list[str]] = {}
    for element in signature_header.split(","):
        key, _, value = element.partition("=")
        if not _:
            raise StripeSignatureError("Malformed Stripe-Signature header")
        parts.setdefault(key.strip(), []).append(value.strip())

    timestamps = parts.get("t")
    if not timestamps:
        raise StripeSignatureError("Stripe-Signature header has no timestamp")
    try:
        timestamp = int(timestamps[0])
    except ValueError:
        raise StripeSignatureError("Stripe-Signature timestamp is not an integer")

    current = int(datetime.now(timezone.utc).timestamp()) if now is None else now
    # Absolute difference, so a header timestamped in the FUTURE is rejected
    # too. A one-sided check would let an attacker who can sign (or who
    # replays during a secret leak) stamp a request far ahead and keep it
    # valid indefinitely.
    if abs(current - timestamp) > tolerance_seconds:
        raise StripeSignatureError("Stripe-Signature timestamp outside the tolerance window")

    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()

    # Stripe sends multiple v1 values during a secret rotation; any one
    # matching is a valid signature. Schemes other than v1 are ignored
    # rather than rejected, so a future scheme Stripe adds alongside v1
    # doesn't break delivery here.
    candidates = parts.get("v1", [])
    if not candidates:
        raise StripeSignatureError("Stripe-Signature header has no v1 signature")
    # compare_digest over every candidate without short-circuiting, so
    # timing doesn't reveal which position matched.
    if not any(hmac.compare_digest(expected, candidate) for candidate in candidates):
        raise StripeSignatureError("Webhook signature does not match")

    return json.loads(payload)


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

    def sign(self, payload: bytes, *, timestamp: int | None = None) -> str:
        """A valid `Stripe-Signature` header for `payload`.

        Exists so tests (and a local dev harness) can exercise the real
        route without hand-rolling the header format — and so they exercise
        the SAME format production verifies, rather than a test-only
        shortcut that could quietly diverge.
        """
        return build_stripe_signature_header(
            payload=payload,
            secret=self.webhook_secret,
            timestamp=(
                int(datetime.now(timezone.utc).timestamp()) if timestamp is None else timestamp
            ),
        )

    def verify_webhook_signature(self, *, payload: bytes, signature_header: str) -> dict[str, Any]:
        # The real header format and the real replay window — the fake is
        # fake about the Stripe *API*, never about the security check.
        return verify_stripe_signature_header(
            payload=payload,
            signature_header=signature_header,
            secret=self.webhook_secret,
        )
