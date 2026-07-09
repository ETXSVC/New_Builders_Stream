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


settings = Settings()
