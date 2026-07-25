"""Every migration's `downgrade()` must actually run.

`alembic upgrade head` is exercised by every test session (conftest's
`_setup_test_database` runs it before anything else), so the upgrade path
cannot rot unnoticed. The downgrade path had nothing exercising it at all —
which matters precisely when it is needed: a bad deploy at 2am, reaching
for `alembic downgrade -1` and discovering that the migration's downgrade
references a column its own upgrade renamed, or drops an index that a
later migration already dropped.

This walks the whole chain to `base` and back to `head` against a scratch
database, which catches:

  * a `downgrade()` that raises (the common case — usually a copy-paste of
    the upgrade's object names, or a DROP of something conditional);
  * a downgrade that leaves an object behind, since the subsequent
    re-upgrade would then fail on "already exists";
  * an ordering mistake, since dropping a table still referenced by a
    later-dropped foreign key fails.

It does NOT assert that a downgrade preserves data — it cannot, and no
downgrade in this codebase claims to. The guarantee here is narrower and
still worth having: the escape hatch works.

Runs against its own throwaway database rather than the shared test one,
because tearing the schema down and rebuilding it underneath the rest of
the suite would be an unpleasant surprise for any test running in parallel.
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest

from tests.conftest import TEST_DATABASE_URL

BACKEND_ROOT = Path(__file__).resolve().parent.parent

_ADMIN_DSN = TEST_DATABASE_URL.replace("+asyncpg", "").rsplit("/", 1)[0] + "/postgres"
_SCRATCH_DB = f"builders_stream_downgrade_{uuid.uuid4().hex[:8]}"


def _alembic(command: list[str], database_url: str) -> subprocess.CompletedProcess:
    """Alembic as a subprocess, not via its Python API.

    The API would need `app.config.settings` re-read with a different
    DATABASE_URL, and that singleton is built once at import time — a
    subprocess with its own environment is the honest way to point Alembic
    at another database, and it also matches how an operator actually runs
    this.

    `sys.executable -m alembic`, not a bare `alembic`: pytest is commonly
    invoked by absolute interpreter path (`/path/to/venv/bin/python -m
    pytest`), which does NOT put that venv's `bin` on PATH, so the console
    script may not be findable even though the package is installed.
    Running it as a module uses the interpreter already running the tests,
    which is the one guaranteed to have it.
    """
    return subprocess.run(
        [sys.executable, "-m", "alembic", *command],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        env={
            # Deliberately minimal: PATH so any subprocess Alembic itself
            # spawns can resolve, and the settings Alembic's env.py needs.
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "MIGRATIONS_DATABASE_URL": database_url,
            "DATABASE_URL": database_url,
            "TEST_DATABASE_URL": database_url,
            "JWT_SECRET": "downgrade-test-secret",
            "INTEGRATION_TOKEN_ENCRYPTION_KEY": "Rewy1h1FRZkZ2sxynenqVW39Vu1r573swS_UOr1uiUk=",
        },
    )


@pytest.fixture
async def scratch_database():
    conn = await asyncpg.connect(_ADMIN_DSN)
    try:
        await conn.execute(f'CREATE DATABASE "{_SCRATCH_DB}"')
    finally:
        await conn.close()

    yield TEST_DATABASE_URL.rsplit("/", 1)[0] + f"/{_SCRATCH_DB}"

    conn = await asyncpg.connect(_ADMIN_DSN)
    try:
        # Migration 0001 creates roles, which are CLUSTER-level, not
        # database-level — they already exist from the main test database's
        # own migration run, and every migration here is written to tolerate
        # that (0001 guards its CREATE ROLE, 0020 uses a DO block). Dropping
        # them on teardown would break the rest of the suite, so the scratch
        # database is dropped and the roles are deliberately left alone.
        await conn.execute(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB}" WITH (FORCE)')
    finally:
        await conn.close()


async def test_every_migration_downgrades_and_re_upgrades_cleanly(scratch_database):
    up = _alembic(["upgrade", "head"], scratch_database)
    assert up.returncode == 0, f"upgrade to head failed:\n{up.stdout}\n{up.stderr}"

    down = _alembic(["downgrade", "base"], scratch_database)
    assert down.returncode == 0, (
        "at least one migration's downgrade() does not run. This is the path an "
        "operator reaches for during a bad deploy, so it failing there is the "
        f"worst possible time to find out:\n{down.stdout}\n{down.stderr}"
    )

    # Re-upgrading is what proves the downgrade actually removed things
    # rather than merely not raising: a leftover table, index or policy
    # surfaces here as "already exists".
    re_up = _alembic(["upgrade", "head"], scratch_database)
    assert re_up.returncode == 0, (
        "the chain does not re-upgrade after a full downgrade, which means a "
        "downgrade left an object behind rather than dropping it:\n"
        f"{re_up.stdout}\n{re_up.stderr}"
    )
