from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_ENV_FILE, extra="ignore")

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


settings = Settings()
