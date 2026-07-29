"""The one database connection the cross-tenant platform console uses.

Directly modelled on `app/tasks/scanner_db.py`, and separate from it on
purpose. Both roles hold BYPASSRLS because both have jobs that span every
tenant, but they are reachable from completely different places and should
not share a privilege set:

  * `scanner` runs in the Dramatiq worker, has no HTTP caller, and holds
    blanket DML so the daily sweeps can write the rows they compute.
  * `platform_admin` runs on the REQUEST path, driven by a human through the
    console. It reads everything and can write exactly three things
    (`company_module_overrides`, `subscriptions`, and an `audit_log` row).
    A bug in a console route therefore cannot corrupt a customer's project,
    estimate or invoice — the privilege simply is not held. See migration
    0023's grants.

`PLATFORM_DATABASE_URL` has NO fallback. `scanner_database_url` falls back
to the owner URL so an existing deployment survives the 0020 upgrade with
its worker running; nothing equivalent applies here, because there is no
older deployment of a feature this new. An unset value leaves `engine` None
and every platform route answers 503 — a loud, correct "not configured"
rather than a silent escalation to whatever connection happened to be handy.
"""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

platform_engine = (
    create_async_engine(settings.platform_database_url, pool_pre_ping=True)
    if settings.platform_database_url
    else None
)

PlatformSessionLocal = (
    async_sessionmaker(platform_engine, expire_on_commit=False, class_=AsyncSession)
    if platform_engine is not None
    else None
)


def platform_console_enabled() -> bool:
    return PlatformSessionLocal is not None
