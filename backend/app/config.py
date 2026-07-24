from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"

# Known committed dev/test secret values, rejected at boot in production.
# Kept as explicit literals (not patterns alone) so every value that has
# ever appeared in this repo's .env.example, tests, or CI workflows is
# individually named — a new dev default must be added here.
_DEV_JWT_SECRETS = frozenset(
    {"dev-only-secret-change-me", "test-secret", "e2e-ci-secret", "ci-test-secret"}
)
_DEV_FERNET_KEYS = frozenset(
    {
        "NHiunJoW7aQN87dHDT9X8r60R79fXf8esa7fKZpW4Bo=",
        "Rewy1h1FRZkZ2sxynenqVW39Vu1r573swS_UOr1uiUk=",
    }
)
_FAKE_WEBHOOK_SECRET = "fake_webhook_secret_for_tests"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_ENV_FILE, extra="ignore")

    # Deployment environment. "production" is the only value that changes
    # behavior: dev-default secrets refuse to boot (validator below) and
    # the OpenAPI/docs endpoints are disabled (app/main.py). The default
    # keeps every local/CI/test path working with zero configuration.
    app_env: Literal["development", "test", "production"] = "development"
    database_url: str
    migrations_database_url: str
    test_database_url: str
    jwt_secret: str
    # docs/07 Section 1: access tokens are short-lived; refresh tokens
    # (Task 6.2+) carry the long-lived session. 15 is the spec's number.
    jwt_expire_minutes: int = 15
    refresh_token_expire_days: int = 14
    redis_url: str = "redis://localhost:6379/0"
    # Local-filesystem root for Document uploads (Task 1.15, design decision
    # #4 in docs/superpowers/plans/2026-07-08-phase-1-crm-project-management.md):
    # Phase 1 stores documents on disk, not S3/object storage, under a
    # Docker-volume-backed path. Default matches the design decision's own
    # example (`STORAGE_ROOT=/data/documents`) and the new `documents_data`
    # volume added to docker-compose.yml, mounted at this exact path in the
    # `backend` service — deliberately NOT under `/app` (the bind-mounted
    # source tree), so uploaded files never land inside the git-tracked
    # backend/ directory. Has a default (like redis_url above) rather than
    # being required, so local/test runs work without every .env needing an
    # explicit override; tests set STORAGE_ROOT to a host-writable temp
    # directory the same way conftest.py overrides DATABASE_URL.
    storage_root: str = "/data/documents"
    # The browser-facing origin of the Next.js app, used only by routes
    # that redirect a BROWSER back into the frontend (today: the
    # integrations OAuth callback). Default matches `npm run dev` outside
    # Docker; docker-compose maps the frontend to host port 3001, so the
    # Compose .env overrides this to http://localhost:3001.
    frontend_base_url: str = "http://localhost:3000"
    # Outbound email (invitation delivery). smtp_host unset (the default)
    # selects the recording fake client — local dev, tests, and CI all send
    # nothing with zero configuration; setting SMTP_HOST is the single
    # switch that turns on real delivery. See app/services/email.py.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_address: str = "no-reply@localhost"
    smtp_starttls: bool = True
    # Task 4.3: Fernet key (44-char urlsafe-base64, generate with
    # `Fernet.generate_key()`) for encrypting integration_connections'
    # access_token_encrypted/refresh_token_encrypted columns at the
    # application layer. Required (no default) — unlike jwt_secret, which
    # is reused for the OAuth state token under a distinct signing context
    # (Task 4.5), this is a genuinely separate secret with a different
    # cryptographic purpose (symmetric encryption, not HMAC signing) and
    # must not be the same value as jwt_secret.
    integration_token_encryption_key: str
    # POST /auth/register anti-enumeration (fast-follow closing the gap
    # documented in docs/superpowers/plans/2026-07-16-frontend-foundation.md's
    # Task 10 review): the endpoint's 409 "Email already registered" lets
    # anyone learn which emails already have an account, at odds with
    # /auth/login's own deliberate constant-time design above. A generic
    # error message wouldn't actually help — the 409 status code alone is
    # still the tell. Rate limiting the endpoint per source IP doesn't
    # eliminate the signal, but makes bulk enumeration impractical without
    # needing an email-verification subsystem this project doesn't have yet.
    register_rate_limit_enabled: bool = True
    register_rate_limit_max_attempts: int = 5
    register_rate_limit_window_seconds: int = 3600
    # Verifies POST /webhooks/stripe signatures (today via FakeStripeClient
    # — see app/services/billing.py). Config-ized so a deployment can use a
    # non-public value even while the fake client stays; the production
    # validator below refuses the committed default.
    stripe_webhook_secret: str = "fake_webhook_secret_for_tests"
    # Root logger level for app/core/logging.py's configure_logging().
    log_level: str = "INFO"
    # Upload byte caps enforced by app/core/uploads.read_upload_limited on
    # every multipart route (except the branding logo, which keeps its own
    # pre-existing 2 MiB cap in document_storage.py). Settings fields, not
    # module constants, so tests can shrink them without multi-MB payloads.
    max_document_upload_bytes: int = 25 * 1024 * 1024
    max_signature_upload_bytes: int = 1 * 1024 * 1024

    @model_validator(mode="after")
    def _refuse_dev_defaults_in_production(self) -> "Settings":
        """Fail-fast at boot when app_env=production carries any known
        dev/test secret. Collects EVERY violation into one error so the
        operator fixes the .env once, not one reboot per secret."""
        if self.app_env != "production":
            return self

        problems: list[str] = []
        if self.jwt_secret in _DEV_JWT_SECRETS or len(self.jwt_secret) < 32:
            problems.append(
                "JWT_SECRET is a known dev value or shorter than 32 characters "
                "(generate one: openssl rand -hex 32)"
            )
        if (
            self.integration_token_encryption_key.startswith("change-me")
            or self.integration_token_encryption_key in _DEV_FERNET_KEYS
        ):
            problems.append(
                "INTEGRATION_TOKEN_ENCRYPTION_KEY is a committed dev value "
                '(generate one: python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())")'
            )
        if self.stripe_webhook_secret == _FAKE_WEBHOOK_SECRET:
            problems.append(
                "STRIPE_WEBHOOK_SECRET is the public fake default "
                "(generate one: openssl rand -hex 32)"
            )
        for field_name in ("database_url", "migrations_database_url"):
            url = getattr(self, field_name)
            if ":devpassword@" in url or ":app_password@" in url:
                problems.append(
                    f"{field_name.upper()} uses a password committed in .env.example "
                    "(set real POSTGRES_PASSWORD/APP_DB_PASSWORD and run "
                    "ALTER ROLE app_user PASSWORD — see docs/11-production-deployment.md)"
                )
        if self.frontend_base_url.startswith("http://localhost"):
            problems.append(
                "FRONTEND_BASE_URL still points at localhost — OAuth redirects and "
                "invitation-email links would send users to their own machine"
            )

        if problems:
            details = "\n".join(f"  - {p}" for p in problems)
            raise ValueError(
                f"APP_ENV=production refused to boot with dev-default configuration:\n{details}"
            )
        return self


# Required (no-default) fields are populated from the environment/.env by
# pydantic-settings at runtime — invisible to mypy's constructor check.
settings = Settings()  # type: ignore[call-arg]
