"""Production fail-fast validator (app/config.py): APP_ENV=production must
refuse to boot with any known dev-default secret, and must name every
violation in one error.

Every test constructs Settings directly with _env_file=None (so the repo's
.env can't leak in) and passes EVERY field it depends on explicitly —
conftest's env vars still fill unpassed fields, so an unpassed field could
silently mask the case under test.
"""
import pytest
from pydantic import ValidationError

from app.config import Settings

_GOOD_PRODUCTION = {
    "app_env": "production",
    "database_url": "postgresql+asyncpg://app_user:S3cureRuntime@postgres:5432/builders_stream",
    "migrations_database_url": "postgresql+asyncpg://postgres:S3cureOwner@postgres:5432/builders_stream",
    "test_database_url": "postgresql+asyncpg://postgres:S3cureOwner@localhost:5432/builders_stream_test",
    "jwt_secret": "a" * 64,
    "integration_token_encryption_key": "x8Zl2kqPz-real-looking-key-value-aaaaaaaaaa=",
    "stripe_webhook_secret": "b" * 64,
    "frontend_base_url": "https://app.example-builder.com",
}


def _settings(**overrides):
    return Settings(_env_file=None, **{**_GOOD_PRODUCTION, **overrides})


def test_good_production_config_boots():
    settings = _settings()
    assert settings.app_env == "production"


def test_development_env_accepts_dev_defaults():
    settings = _settings(
        app_env="development",
        jwt_secret="dev-only-secret-change-me",
        integration_token_encryption_key="change-me-generate-a-real-fernet-key=",
        stripe_webhook_secret="fake_webhook_secret_for_tests",
        database_url="postgresql+asyncpg://app_user:app_password@localhost:5432/builders_stream",
        frontend_base_url="http://localhost:3000",
    )
    assert settings.app_env == "development"


@pytest.mark.parametrize(
    "overrides,expected_fragment",
    [
        ({"jwt_secret": "dev-only-secret-change-me"}, "JWT_SECRET"),
        ({"jwt_secret": "short-but-not-a-dev-value"}, "JWT_SECRET"),
        (
            {"integration_token_encryption_key": "change-me-generate-a-real-fernet-key="},
            "INTEGRATION_TOKEN_ENCRYPTION_KEY",
        ),
        (
            {"integration_token_encryption_key": "Rewy1h1FRZkZ2sxynenqVW39Vu1r573swS_UOr1uiUk="},
            "INTEGRATION_TOKEN_ENCRYPTION_KEY",
        ),
        ({"stripe_webhook_secret": "fake_webhook_secret_for_tests"}, "STRIPE_WEBHOOK_SECRET"),
        (
            {"database_url": "postgresql+asyncpg://app_user:app_password@postgres:5432/builders_stream"},
            "DATABASE_URL",
        ),
        (
            {
                "migrations_database_url": "postgresql+asyncpg://postgres:devpassword@postgres:5432/builders_stream"
            },
            "MIGRATIONS_DATABASE_URL",
        ),
        ({"frontend_base_url": "http://localhost:3001"}, "FRONTEND_BASE_URL"),
    ],
)
def test_each_dev_default_is_rejected_in_production(overrides, expected_fragment):
    with pytest.raises(ValidationError) as excinfo:
        _settings(**overrides)
    assert expected_fragment in str(excinfo.value)


def test_all_violations_reported_in_one_error():
    """One boot failure lists everything — the operator fixes the .env
    once, not one reboot per secret."""
    with pytest.raises(ValidationError) as excinfo:
        _settings(
            jwt_secret="dev-only-secret-change-me",
            stripe_webhook_secret="fake_webhook_secret_for_tests",
            frontend_base_url="http://localhost:3001",
        )
    message = str(excinfo.value)
    assert "JWT_SECRET" in message
    assert "STRIPE_WEBHOOK_SECRET" in message
    assert "FRONTEND_BASE_URL" in message
