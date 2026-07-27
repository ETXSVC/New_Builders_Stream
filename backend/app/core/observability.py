"""Error reporting, off unless someone points it at a Sentry instance.

`docs/11-production-deployment.md` has carried Sentry as a deferred
follow-up on the grounds that there was no instance to configure. That
reason blocks *configuring* it, not *shipping* it: with no `SENTRY_DSN`
set, `init_error_reporting()` returns immediately and the dependency sits
unused. Setting one environment variable turns it on, which is a far
better position than a snippet in a runbook that has to be transcribed
correctly during an incident.

## What this deliberately does not send

This system holds material that must not leave it casually: client email
addresses, the IP addresses captured as legal evidence of e-signature
acceptance (docs/07), and per-tenant financial records. Sentry's
`send_default_pii` defaults to False; it is set explicitly below anyway,
because the cost of that default silently changing in a future SDK
release is an ESIGN IP address in a third-party service.

`before_send` then strips two more things the default scrubber does not
know about:

  * this codebase's own secret NAMES (`integration_token_encryption_key`,
    `jwt_secret`, `stripe_webhook_secret`) wherever they appear in
    local-variable frames — Sentry's default denylist covers `password`
    and `token`, not these;
  * the `Authorization`, `Cookie` and `X-Tenant-ID` request headers.
    The first two are obvious. `X-Tenant-ID` is not sensitive in itself,
    but it is attacker-*controlled* (design decision #3), so an event's
    header value is not evidence of which tenant was actually served —
    the `company_id` tag set by the middleware is, because that value has
    been through the membership check.

## What it does send

`app_env` as the environment, so staging noise never lands in the
production feed, and a `company_id` tag when the request had a verified
tenant. That tag is the difference between "some 500s happened" and "one
company is broken", and a company UUID is not personal data.

Tracing is off by default (`traces_sample_rate=0.0`). Errors are the
stated need; performance traces on a single-box deployment mostly cost
quota. `SENTRY_TRACES_SAMPLE_RATE` raises it when someone actually wants
them.
"""
import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# Config field names whose VALUES must never appear in an event. Matched
# against local-variable names in captured frames.
_SECRET_NAMES = frozenset(
    {
        "jwt_secret",
        "integration_token_encryption_key",
        "stripe_webhook_secret",
        "smtp_password",
        "database_url",
        "migrations_database_url",
        "scanner_database_url",
    }
)

_SCRUBBED_HEADERS = frozenset({"authorization", "cookie", "x-tenant-id"})

_REDACTED = "[redacted]"


def _scrub(event: Any, _hint: Any) -> Any:
    # `Any` rather than sentry_sdk's own `Event` type: sentry-sdk is an
    # OPTIONAL dependency (see pyproject's `observability` extra), so this
    # module must both import and type-check without it installed.
    """Remove this codebase's secrets before the event leaves the process."""
    request = event.get("request")
    if isinstance(request, dict):
        headers = request.get("headers")
        if isinstance(headers, dict):
            for name in list(headers):
                if name.lower() in _SCRUBBED_HEADERS:
                    headers[name] = _REDACTED

    for exception in (event.get("exception") or {}).get("values") or []:
        for frame in (exception.get("stacktrace") or {}).get("frames") or []:
            local_vars = frame.get("vars")
            if not isinstance(local_vars, dict):
                continue
            for name in list(local_vars):
                if name in _SECRET_NAMES:
                    local_vars[name] = _REDACTED
    return event


def init_error_reporting(component: str) -> bool:
    """Initialise Sentry if a DSN is configured. Returns whether it did.

    `component` ("api", "worker", "scheduler") is attached as a tag: the
    three processes fail in different ways and share a codebase, so an
    event that cannot say which one it came from costs triage time.

    Import is local rather than module-level so the dependency stays
    genuinely optional — a deployment that never sets a DSN does not pay
    the import, and a missing package degrades to a warning rather than
    refusing to boot.
    """
    if not settings.sentry_dsn:
        return False

    try:
        import sentry_sdk
    except ImportError:
        logger.warning(
            "SENTRY_DSN is set but sentry-sdk is not installed; error reporting is OFF"
        )
        return False

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        # See the module docstring. Explicit despite matching the current
        # default, because this one changing under us would be an ESIGN IP
        # address in someone else's database.
        send_default_pii=False,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        before_send=_scrub,
    )
    sentry_sdk.set_tag("component", component)
    logger.info("error reporting enabled for component=%s env=%s", component, settings.app_env)
    return True


def tag_current_tenant(company_id: Any, role: str) -> None:
    """Attach the verified tenant and role to the current event scope.

    Called from `get_current_user` AFTER the membership check, so the value
    is what the request was actually served as, not what its header
    claimed. A company UUID and a role name are not personal data, and they
    are the difference between an alert that says "500s are up" and one
    that says "this company cannot invoice".

    Silent no-op when Sentry is not configured — this runs on every
    authenticated request, so it must cost nothing and must never be the
    reason a request fails.
    """
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk

        sentry_sdk.set_tag("company_id", str(company_id))
        sentry_sdk.set_tag("role", role)
    except Exception:  # pragma: no cover - telemetry must never break a request
        logger.debug("could not tag the Sentry scope", exc_info=True)
