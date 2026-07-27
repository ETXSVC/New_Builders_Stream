"""Error reporting is off by default, and never leaks this app's secrets.

Sentry is optional: `app/core/observability.py` returns early when
`SENTRY_DSN` is unset, and degrades to a warning when the DSN is set but
`sentry-sdk` is not installed. Both of those are exercised here, because
"telemetry is off" is a claim a deployment relies on.

The scrubber tests matter more than they look. This system holds client
email addresses and the IP addresses captured as legal evidence of
e-signature acceptance (docs/07), so an event carrying an Authorization
header or a Fernet key into a third-party service is a real incident, not
a tidiness problem.
"""
from app.core import observability


def test_reporting_is_off_when_no_dsn_is_configured(monkeypatch):
    monkeypatch.setattr(observability.settings, "sentry_dsn", None)
    assert observability.init_error_reporting("api") is False


def test_a_dsn_without_the_package_warns_rather_than_crashing(monkeypatch):
    """A deployment that sets SENTRY_DSN but skips the `observability`
    extra must still boot. Refusing to start because telemetry is
    unavailable would make the monitoring the outage."""
    monkeypatch.setattr(observability.settings, "sentry_dsn", "https://x@example.test/1")
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def _no_sentry(name, *args, **kwargs):
        if name == "sentry_sdk":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _no_sentry)
    assert observability.init_error_reporting("api") is False


def test_tagging_is_a_no_op_when_reporting_is_off(monkeypatch):
    """Runs on every authenticated request, so it must cost nothing and
    must never be the reason a request fails."""
    monkeypatch.setattr(observability.settings, "sentry_dsn", None)
    observability.tag_current_tenant("11111111-1111-1111-1111-111111111111", "admin")


def test_scrubber_removes_sensitive_request_headers():
    event = {
        "request": {
            "headers": {
                "Authorization": "Bearer real-token",
                "Cookie": "refresh_token=abc",
                "X-Tenant-ID": "11111111-1111-1111-1111-111111111111",
                "User-Agent": "Mozilla/5.0",
            }
        }
    }
    scrubbed = observability._scrub(event, {})
    headers = scrubbed["request"]["headers"]
    assert headers["Authorization"] == "[redacted]"
    assert headers["Cookie"] == "[redacted]"
    # Not sensitive in itself, but attacker-CONTROLLED (design decision #3),
    # so its value is not evidence of which tenant was served — the
    # company_id tag set after the membership check is.
    assert headers["X-Tenant-ID"] == "[redacted]"
    # Non-sensitive headers survive, or the events are useless.
    assert headers["User-Agent"] == "Mozilla/5.0"


def test_scrubber_removes_this_codebases_secrets_from_stack_frames():
    """Sentry's default denylist knows `password` and `token`. It does not
    know `integration_token_encryption_key` — the key that decrypts every
    tenant's stored OAuth credentials — so this codebase names its own."""
    event = {
        "exception": {
            "values": [
                {
                    "stacktrace": {
                        "frames": [
                            {
                                "vars": {
                                    "jwt_secret": "super-secret-signing-key",
                                    "integration_token_encryption_key": "fernet-key",
                                    "database_url": "postgresql://user:pw@host/db",
                                    "company_name": "Acme Construction",
                                }
                            }
                        ]
                    }
                }
            ]
        }
    }
    frame_vars = observability._scrub(event, {})["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
    assert frame_vars["jwt_secret"] == "[redacted]"
    assert frame_vars["integration_token_encryption_key"] == "[redacted]"
    assert frame_vars["database_url"] == "[redacted]"
    # Ordinary locals must survive — a scrubber that eats everything makes
    # the traceback worthless and people turn it off.
    assert frame_vars["company_name"] == "Acme Construction"


def test_scrubber_tolerates_events_without_the_shapes_it_looks_for():
    """before_send runs on every event, including ones with no request and
    no stacktrace. Raising here would drop the event entirely."""
    assert observability._scrub({}, {}) == {}
    assert observability._scrub({"request": None, "exception": None}, {}) is not None
