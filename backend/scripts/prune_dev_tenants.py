"""Delete throwaway tenants from a development database.

    python scripts/prune_dev_tenants.py              # dry run: report only
    python scripts/prune_dev_tenants.py --yes        # actually delete
    python scripts/prune_dev_tenants.py --company-like 'Demo %' --yes

`npm run test:e2e` registers a fresh company per spec file and never cleans
up — Playwright has no teardown — so a dev database accumulates a tenant
thicket (~29 companies and ~37 users per full run, cumulative). That makes
`\\dt`-level spelunking and "why is this tier gate behaving oddly" debugging
noticeably worse. This removes them.

WHY THIS IS A SCRIPT AND NOT A PLAYWRIGHT `globalTeardown`: the teardown
would need a Postgres driver in the frontend (it has none) and the foreign
key ordering knowledge below, which belongs on this side of the fence. It
would also delete unconditionally — including the run a developer is halfway
through investigating — and buy nothing in CI, where the database is thrown
away with the job. Pruning is an operator action, so it gets an operator
command.

Like `grant_platform_admin.py` this runs as the table OWNER, via
`MIGRATIONS_DATABASE_URL`: it deletes across every tenant, which is the one
thing `app_user` must never be able to do. That is also why it lives in
`scripts/` and not `app/` — `tests/test_worker_db_roles.py` sweeps `app/` for
exactly this URL and would (correctly) fail the build if it appeared there.
"""
import argparse
import asyncio
import sys
import uuid

import asyncpg

from app.config import settings

DEFAULT_COMPANY_LIKE = "E2E %"
DEFAULT_EMAIL_LIKE = "e2e-%"


def _dsn() -> str:
    return settings.migrations_database_url.replace("+asyncpg", "")


async def _target_companies(conn, company_like: str) -> list[uuid.UUID]:
    """Matching companies plus every descendant.

    Today's e2e tenants are all roots, so the recursion is redundant — but a
    tenant tree is what `companies.parent_id` exists for, and a prune that
    silently skipped a child branch would leave rows whose parent is gone.
    """
    rows = await conn.fetch(
        """
        WITH RECURSIVE seed AS (
            SELECT id FROM companies WHERE name LIKE $1
            UNION
            SELECT c.id FROM companies c JOIN seed s ON c.parent_id = s.id
        )
        SELECT id FROM seed
        """,
        company_like,
    )
    return [row["id"] for row in rows]


async def _tenant_tables(conn) -> list[str]:
    """Every base table with a `company_id`, straight from the catalog.

    Catalog-driven rather than a transcribed list, for the same reason
    `test_rls_policy_coverage.py` is: a table added next month is covered
    without anyone remembering to edit this file.
    """
    rows = await conn.fetch(
        """
        SELECT c.table_name
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE c.column_name = 'company_id'
          AND c.table_schema = 'public'
          AND t.table_type = 'BASE TABLE'
        ORDER BY c.table_name
        """
    )
    return [row["table_name"] for row in rows]


async def _delete_tenant_rows(conn, tables: list[str], ids: list[uuid.UUID]) -> dict[str, int]:
    """Delete child rows, retrying until the FK graph is satisfied.

    Only `company_users` and `invitations` CASCADE from `companies`; every
    other `company_id` FK is NO ACTION, and the children reference each other
    (`estimate_line_items` → `estimates`, `bom_lines` → `bom_line_receipts`,
    ...). Rather than hard-code a topological order that a new table would
    invalidate, delete what currently deletes cleanly and go round again.
    Each attempt gets a savepoint so a violation doesn't poison the outer
    transaction.
    """
    deleted: dict[str, int] = {}
    pending = set(tables)
    while pending:
        cleared = set()
        for table in sorted(pending):
            try:
                async with conn.transaction():
                    result = await conn.execute(
                        f'DELETE FROM "{table}" WHERE company_id = ANY($1::uuid[])', ids
                    )
            except asyncpg.ForeignKeyViolationError:
                continue  # a child in `pending` still points here; next pass
            count = int(result.split()[-1])
            if count:
                deleted[table] = count
            cleared.add(table)
        if not cleared:
            raise RuntimeError(
                "Cannot make progress deleting: "
                + ", ".join(sorted(pending))
                + ". A foreign key cycle, or a reference from a company outside the "
                "prune set — inspect before forcing."
            )
        pending -= cleared
    return deleted


async def _delete_users(conn, email_like: str) -> tuple[int, list[str]]:
    """Delete matching users left with no membership; report those still referenced.

    A user can outlive the prune legitimately: `audit_log.actor_id`,
    `documents.uploaded_by` and friends are NO ACTION, so an e2e user who
    touched a company we are keeping must stay. Deleting per-user with a
    savepoint turns that into a reported skip instead of a failed run.
    """
    rows = await conn.fetch(
        """
        SELECT u.id, u.email FROM users u
        WHERE u.email LIKE $1
          AND NOT EXISTS (SELECT 1 FROM company_users cu WHERE cu.user_id = u.id)
        ORDER BY u.email
        """,
        email_like,
    )
    deleted, skipped = 0, []
    for row in rows:
        try:
            async with conn.transaction():
                await conn.execute("DELETE FROM users WHERE id = $1", row["id"])
        except asyncpg.ForeignKeyViolationError:
            skipped.append(row["email"])
            continue
        deleted += 1
    return deleted, skipped


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--company-like",
        default=DEFAULT_COMPANY_LIKE,
        help=f"SQL LIKE pattern for company names (default: {DEFAULT_COMPANY_LIKE!r})",
    )
    parser.add_argument(
        "--email-like",
        default=DEFAULT_EMAIL_LIKE,
        help=f"SQL LIKE pattern for user emails (default: {DEFAULT_EMAIL_LIKE!r})",
    )
    parser.add_argument(
        "--yes", action="store_true", help="perform the deletion (default is a dry run)"
    )
    args = parser.parse_args()

    # A prune that crosses every tenant has no business pointing at production,
    # and the guard is cheap. `app_env` is already fail-fast validated at boot.
    if settings.app_env == "production":
        print("Refusing to run with app_env=production.", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(_dsn())
    try:
        database = await conn.fetchval("SELECT current_database()")
        ids = await _target_companies(conn, args.company_like)
        if not ids:
            print(f"No companies match {args.company_like!r} in {database}. Nothing to do.")
            return 0

        print(f"{len(ids)} companies match {args.company_like!r} in {database}.")
        if not args.yes:
            tables = await _tenant_tables(conn)
            total = 0
            for table in tables:
                count = await conn.fetchval(
                    f'SELECT count(*) FROM "{table}" WHERE company_id = ANY($1::uuid[])', ids
                )
                if count:
                    print(f"  {table:<28} {count:>6}")
                    total += count
            users = await conn.fetchval(
                """
                SELECT count(*) FROM users u
                WHERE u.email LIKE $1
                  AND NOT EXISTS (SELECT 1 FROM company_users cu WHERE cu.user_id = u.id
                                  AND cu.company_id <> ALL($2::uuid[]))
                """,
                args.email_like,
                ids,
            )
            print(f"  {'(users)':<28} {users:>6}")
            print(f"\nDry run: {total} child rows + {len(ids)} companies + {users} users.")
            print("Re-run with --yes to delete.")
            return 0

        async with conn.transaction():
            tables = await _tenant_tables(conn)
            deleted = await _delete_tenant_rows(conn, tables, ids)
            companies = await conn.execute("DELETE FROM companies WHERE id = ANY($1::uuid[])", ids)
            users, skipped = await _delete_users(conn, args.email_like)

        for table, count in sorted(deleted.items()):
            print(f"  {table:<28} {count:>6}")
        print(f"  {'companies':<28} {int(companies.split()[-1]):>6}")
        print(f"  {'users':<28} {users:>6}")
        if skipped:
            print(
                f"\nKept {len(skipped)} user(s) still referenced by a company outside the "
                f"prune set: {', '.join(skipped)}"
            )
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
