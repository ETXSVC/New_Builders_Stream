from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_ENV_FILE, extra="ignore")

    database_url: str
    migrations_database_url: str
    test_database_url: str
    jwt_secret: str
    jwt_expire_minutes: int = 60
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


settings = Settings()
