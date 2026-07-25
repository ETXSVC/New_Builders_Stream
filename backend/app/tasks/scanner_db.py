"""The one database connection the cross-tenant daily sweeps use.

Three jobs in this package are genuinely cross-tenant by nature — the
compliance-expiry scan, the seat-usage report, and the overdue
financial-record sweep each have to look at every company in one pass,
with no caller and no single tenant to scope to.

They used to get that reach by each building their own engine from
`settings.migrations_database_url`, i.e. the `postgres` role that OWNS
every table. That role is exempt from RLS, and can also drop tables, alter
policies and disable row security — so the whole async job layer held the
highest privilege in the database to do work that only ever needs to read
and write rows.

`scanner` (migration 0020) is LOGIN + BYPASSRLS with DML grants and no
ownership. It still sees every tenant, because that is the job; it can no
longer modify the schema or the policies that protect it. Single-tenant
jobs do not belong here at all — `accounting_sync` resolves its tenant and
runs under the ordinary RLS-constrained `app_user`.

One engine for all three, rather than one per module: these modules are
imported together by the worker's actor list *and* by the scheduler, and
five module-level engines meant every one of those processes opened five
pools at import time to use at most one.

`SCANNER_DATABASE_URL` falls back to `migrations_database_url` when unset,
so an existing deployment survives the upgrade — see that setting's comment
in `app/config.py`. The fallback is a migration aid; the runbook's env
table lists the real value.
"""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

_scanner_url = settings.scanner_database_url or settings.migrations_database_url

scanner_engine = create_async_engine(_scanner_url, pool_pre_ping=True)
ScannerSessionLocal = async_sessionmaker(
    scanner_engine, expire_on_commit=False, class_=AsyncSession
)
