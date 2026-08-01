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
from collections.abc import Iterator
from contextlib import contextmanager
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


class StripeConfigurationError(Exception):
    """Raised at construction when RealStripeClient cannot possibly work.

    Deliberately raised from `__post_init__` rather than from the first API
    call: app/services/billing.py builds the client at import time, so this
    surfaces as a failed boot, which is loud, rather than as a failed
    registration for one unlucky customer, which is quiet.
    """


class StripeUnavailableError(Exception):
    """Any failure reaching Stripe or any error Stripe returned.

    Exists so nothing outside this module has to import `stripe` to catch a
    Stripe failure — which is what keeps the SDK an optional extra. The
    handler in app/main.py turns it into a 502: the request failed because a
    dependency of ours did, which is not the caller's fault and not a bug in
    our own code, and the two 5xx codes say different things to whoever
    reads the logs.
    """


@dataclass
class RealStripeClient:
    """The SDK-backed StripeClient — the implementation that moves money.

    Selected only when STRIPE_API_KEY is set (app/services/billing.py);
    FakeStripeClient remains the default everywhere else, so tests, CI and a
    local `docker compose up` still need no Stripe account and make no
    network calls.

    Three things here are specific to the modern Stripe API and are the
    reason this could not be written from memory against the docs as they
    stood when the billing spec was drafted:

    * **`client.v1.*`, not `client.*`.** The un-namespaced accessors still
      work in stripe-python 15.x but emit a DeprecationWarning on every
      call; `v1` is where the SDK has moved.
    * **`current_period_end` is no longer on the Subscription object.** As of
      API version 2025-03-31.basil it lives on each *subscription item*,
      because different items on one subscription can bill on different
      cycles. `_period_end_of` below reads it from the item, preferring
      `trial_end` while the subscription is still trialing — which is the
      same instant, and is what the billing spec means by "current_period_end
      = trial end".
    * **The async methods need `httpx`.** The SDK's sync path uses
      `requests`; `*_async` refuses to run without an async HTTP client and
      raises a bare ImportError from inside the first API call. Checked in
      `__post_init__` instead, because the natural place to discover it
      otherwise is a customer's first registration against a production
      image (the dev extra carries httpx, so every test and CI job has it —
      exactly the shape of gap backend-ci's docker-build job exists to catch).
    """

    api_key: str
    webhook_secret: str
    # Stripe Price id per tier — the base line item on a new subscription.
    tier_price_ids: dict[str, str]
    # Where the hosted Customer Portal returns the user to.
    portal_return_url: str
    # Per-unit Price for seats beyond `included_seats`. Optional: unset means
    # overage reporting is a no-op (see report_seat_usage).
    seat_overage_price_id: str | None = None

    def __post_init__(self) -> None:
        # Every tier the caller passed must carry a Price. The set of tiers
        # is the caller's to know (app/services/billing.py owns
        # TIER_INCLUDED_SEATS, and importing it here would be a cycle), so
        # this checks the dict it was handed rather than a second list of
        # tier names that could drift out of step with the first.
        missing = sorted(tier for tier, price in self.tier_price_ids.items() if not price)
        if missing:
            raise StripeConfigurationError(
                "STRIPE_API_KEY is set, so RealStripeClient is in use, but no Stripe Price "
                f"is configured for tier(s): {', '.join(missing)}. Set "
                + ", ".join(f"STRIPE_PRICE_ID_{t.upper()}" for t in missing)
            )
        if not self.portal_return_url:
            raise StripeConfigurationError(
                "STRIPE_PORTAL_RETURN_URL (or FRONTEND_BASE_URL) must be set — Stripe's "
                "hosted portal rejects a session created without a return_url"
            )
        try:
            import httpx  # noqa: F401
        except ImportError:
            raise StripeConfigurationError(
                "The Stripe SDK's async API requires httpx, which is not installed. "
                "Install the backend's `stripe` extra (pip install '.[stripe]') rather "
                "than the bare `stripe` package"
            )

        import stripe

        # One client for the process. The SDK holds no per-request state and
        # its HTTP client pools connections, so sharing it is both correct
        # and what keeps a per-request TLS handshake off the hot path.
        self._stripe = stripe
        self._client = stripe.StripeClient(self.api_key)

    async def create_customer(self, *, email: str, name: str) -> str:
        # Idempotency key derived from the email, which is what makes this
        # safe to retry: app/routers/auth.py deliberately calls Stripe BEFORE
        # opening its transaction, and accepts that a registration failing
        # after this point leaves a customer behind. Without a key, a user
        # who submits twice (or retries after a 409) accretes a new Stripe
        # customer each time. Stripe expires keys after 24h, so a genuine
        # re-registration much later still gets a fresh customer.
        with self._wrap():
            customer = await self._client.v1.customers.create_async(
                params={"email": email, "name": name},
                options={"idempotency_key": f"create_customer:{email}"},
            )
        return customer.id

    async def create_trialing_subscription(
        self, *, customer_id: str, tier: str, trial_days: int
    ) -> StripeSubscription:
        items: list[dict[str, Any]] = [{"price": self.tier_price_ids[tier], "quantity": 1}]
        if self.seat_overage_price_id:
            # Added at creation with quantity 0 so the daily seat-usage job
            # only ever has to UPDATE an existing item. Adding the item
            # lazily on first overage instead would make that job's write
            # path depend on whether it had ever run before.
            items.append({"price": self.seat_overage_price_id, "quantity": 0})

        # Typed as Any because the SDK's own TypedDict for this call
        # (`SubscriptionCreateParams`) can only be imported from `stripe`,
        # which is an optional extra — importing it at module scope to
        # satisfy mypy would make the whole module unimportable without it,
        # which is the property this client is built around.
        params: Any = {
            "customer": customer_id,
            "items": items,
            "trial_period_days": trial_days,
            # A trial that reaches its end with no card on file cancels,
            # rather than Stripe's default of raising an invoice nobody can
            # pay. Cancellation is a state this product already models end
            # to end: the customer.subscription.deleted webhook writes
            # status="canceled", and block_if_read_only turns that into
            # read-only access rather than a broken account.
            "trial_settings": {"end_behavior": {"missing_payment_method": "cancel"}},
        }

        with self._wrap():
            subscription = await self._client.v1.subscriptions.create_async(
                params=params,
                options={"idempotency_key": f"create_trial:{customer_id}"},
            )

        return StripeSubscription(
            stripe_subscription_id=subscription.id,
            status=subscription.status,
            current_period_end=self._period_end_of(subscription),
        )

    async def create_portal_session(self, *, customer_id: str) -> str:
        with self._wrap():
            session = await self._client.v1.billing_portal.sessions.create_async(
                params={"customer": customer_id, "return_url": self.portal_return_url}
            )
        return session.url

    async def report_seat_usage(self, *, stripe_subscription_id: str, quantity: int) -> None:
        """Set the seat-overage line item's quantity to `quantity`.

        **Set semantics, not increment** — and that is the whole design
        decision here, left open by app/tasks/seat_usage.py's own docstring.
        That job is a daily *snapshot*: it recomputes the full overage from
        scratch every run and reports it. Stripe's meter-event API
        (`billing.meter_events`) accumulates within a billing period, so
        pointing a daily snapshot at it would bill roughly thirty times the
        real overage by the end of a month. Updating a licensed per-unit
        item's `quantity` is idempotent by construction: reporting 2 on
        thirty consecutive days bills 2 seats, and a re-run after a failure
        cannot double-charge.

        `proration_behavior="none"` for the same reason — each day's write
        would otherwise raise a prorated adjustment, so a company that added
        and removed a seat would be billed for both movements instead of for
        where it ended up. The last snapshot before the period closes is
        what invoices.
        """
        if not self.seat_overage_price_id:
            # No overage Price configured: nothing to report against. A
            # no-op rather than an error, so an account that has not set up
            # overage pricing runs the daily job harmlessly instead of
            # logging a failure every night.
            return

        with self._wrap():
            subscription = await self._client.v1.subscriptions.retrieve_async(
                stripe_subscription_id
            )
            item_id = next(
                (
                    item.id
                    for item in subscription["items"].data
                    if item.price and item.price.id == self.seat_overage_price_id
                ),
                None,
            )
            if item_id is None:
                # The subscription predates the overage Price being
                # configured. Skipping is right: silently adding a billable
                # line item to an existing subscription from a nightly job
                # is not a decision this job should make on its own.
                return

            await self._client.v1.subscription_items.update_async(
                item_id,
                params={"quantity": quantity, "proration_behavior": "none"},
            )

    def verify_webhook_signature(self, *, payload: bytes, signature_header: str) -> dict[str, Any]:
        # The shared verifier, NOT stripe.Webhook.construct_event — the same
        # code path the fake uses and the whole test suite exercises. The
        # header format and the replay window are Stripe's, not an
        # implementation's (see this module's docstring), so there is one
        # implementation of the security check rather than one that ships and
        # one that is tested.
        return verify_stripe_signature_header(
            payload=payload,
            signature_header=signature_header,
            secret=self.webhook_secret,
        )

    @contextmanager
    def _wrap(self) -> Iterator[None]:
        """Translate every `stripe` exception into StripeUnavailableError.

        So no caller — router, event handler or task — needs to import the
        SDK to handle a Stripe failure, which is what lets `stripe` stay an
        optional extra rather than a hard dependency.
        """
        try:
            yield
        except self._stripe.StripeError as exc:
            raise StripeUnavailableError(str(exc)) from exc

    def _period_end_of(self, subscription: Any) -> datetime:
        """The end of the period this subscription is currently in.

        `trial_end` first: while trialing, that IS the current period end,
        and it is present on the subscription itself. Otherwise fall back to
        the first item's `current_period_end` — see the class docstring for
        why it is no longer on the subscription object.
        """
        epoch = subscription.get("trial_end")
        if epoch is None:
            items = subscription["items"].data
            epoch = items[0].current_period_end if items else None
        if epoch is None:
            raise StripeUnavailableError(
                f"Stripe subscription {subscription.id} has neither a trial_end nor an "
                "item with a current_period_end; cannot determine the period end"
            )
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
