#!/usr/bin/env python3
"""Runbook §4 item 1: APP_ENV=production refuses to boot on dev defaults.

Needs no database, no Redis and no running stack — it constructs `Settings`
directly. Run it before anything else on a new box: it is thirty seconds,
and it proves the guard that protects every other secret in your `.env`.

    python scripts/smoke/check_production_config.py

Every `Settings(...)` here passes `_env_file=None` so the repository's own
`.env` cannot leak in and quietly satisfy a field the test meant to leave
wrong — the same discipline `tests/test_config_validation.py` uses.

The first assertion is the one people leave out: a fully-configured
production `.env` must actually BOOT. A validator that refuses everything
would pass all twelve rejection cases below and be worthless.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Importable whether or not the package is installed, and without assuming
# the caller's working directory — `backend/` is three parents up.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pydantic import ValidationError  # noqa: E402

from app.config import Settings  # noqa: E402

# A production configuration with nothing wrong with it. Each case below
# breaks exactly one thing about this.
GOOD = dict(
    app_env="production",
    database_url="postgresql+asyncpg://app_user:s3cret-real-pw@postgres:5432/bs",
    migrations_database_url="postgresql+asyncpg://postgres:s3cret-real-pw@postgres:5432/bs",
    test_database_url="postgresql+asyncpg://postgres:s3cret-real-pw@postgres:5432/bs",
    jwt_secret="a" * 64,
    integration_token_encryption_key="EpppgGvVQ2rXcJmKzT0oPq8bN4wLxYh6uZfA1sD3eGk=",
    stripe_webhook_secret="b" * 64,
    frontend_base_url="https://app.example.com",
)

# (label, overrides, substring the error must name)
CASES = [
    ("known dev JWT secret", {"jwt_secret": "dev-only-secret-change-me"}, "JWT_SECRET"),
    ("JWT secret under 32 chars", {"jwt_secret": "tooshort"}, "JWT_SECRET"),
    (
        "committed Fernet key",
        {"integration_token_encryption_key": "NHiunJoW7aQN87dHDT9X8r60R79fXf8esa7fKZpW4Bo="},
        "INTEGRATION_TOKEN_ENCRYPTION_KEY",
    ),
    (
        "placeholder Fernet key",
        {"integration_token_encryption_key": "change-me-please"},
        "INTEGRATION_TOKEN_ENCRYPTION_KEY",
    ),
    (
        "public fake webhook secret",
        {"stripe_webhook_secret": "fake_webhook_secret_for_tests"},
        "STRIPE_WEBHOOK_SECRET",
    ),
    (
        "devpassword in the DB URL",
        {"database_url": "postgresql+asyncpg://app_user:devpassword@postgres:5432/bs"},
        "DATABASE_URL",
    ),
    (
        "migration 0001's app_password still in use",
        {"database_url": "postgresql+asyncpg://app_user:app_password@postgres:5432/bs"},
        "DATABASE_URL",
    ),
    (
        "FRONTEND_BASE_URL still localhost",
        {"frontend_base_url": "http://localhost:3000"},
        "FRONTEND_BASE_URL",
    ),
    (
        "encryption key reused as the JWT secret",
        {"integration_token_encryption_key": "c" * 64, "jwt_secret": "c" * 64},
        "same value as JWT_SECRET",
    ),
    (
        "SMTP credentials over cleartext",
        {"smtp_host": "mail.example.com", "smtp_username": "u", "smtp_starttls": False},
        "SMTP_STARTTLS",
    ),
]


def build(**overrides) -> Settings:
    return Settings(_env_file=None, **{**GOOD, **overrides})  # type: ignore[arg-type]


def main() -> int:
    failures: list[str] = []

    def record(ok: bool, label: str, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(label + (f" — {detail}" if detail else ""))

    print("\n[item 1] Production config fail-fast\n")

    # Non-vacuity: if this fails, every rejection below proves nothing.
    try:
        build()
        record(True, "a fully-configured production .env boots")
    except ValidationError as exc:
        record(False, "a fully-configured production .env boots", str(exc)[:200])

    for label, override, marker in CASES:
        try:
            build(**override)
        except ValidationError as exc:
            record(marker in str(exc), f"refused: {label}", "" if marker in str(exc) else f"message did not name {marker!r}")
        else:
            record(False, f"refused: {label}", "BOOTED — the validator missed it")

    # The documented promise: one error listing everything, so the operator
    # fixes the .env once rather than rebooting per secret.
    try:
        build(
            jwt_secret="dev-only-secret-change-me",
            stripe_webhook_secret="fake_webhook_secret_for_tests",
            frontend_base_url="http://localhost:3000",
        )
        record(False, "three simultaneous violations reported at once", "booted")
    except ValidationError as exc:
        listed = sum(
            m in str(exc) for m in ("JWT_SECRET", "STRIPE_WEBHOOK_SECRET", "FRONTEND_BASE_URL")
        )
        record(listed == 3, "three simultaneous violations reported in ONE error", f"{listed}/3 listed")

    # And the whole mechanism must be inert outside production.
    try:
        Settings(_env_file=None, **{**GOOD, "app_env": "development", "jwt_secret": "short"})  # type: ignore[arg-type]
        record(True, "the same bad config is accepted under APP_ENV=development")
    except ValidationError:
        record(False, "the same bad config is accepted under APP_ENV=development", "blocked")

    total = len(CASES) + 3
    print()
    if failures:
        print(f"ITEM 1: FAIL — {len(failures)} of {total} assertions failed")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"ITEM 1: PASS — {total}/{total} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
