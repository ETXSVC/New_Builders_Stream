"""Grant, list or revoke platform-administrator privilege.

    python scripts/grant_platform_admin.py list
    python scripts/grant_platform_admin.py grant  ops@example.com
    python scripts/grant_platform_admin.py revoke ops@example.com

THIS SCRIPT EXISTS BECAUSE NO ROUTE DOES. `platform_admins` (migration
0023) revokes INSERT/UPDATE/DELETE from both `app_user` (the request path)
and `scanner` (the worker), so there is no code path reachable from an HTTP
request or a background job that can grant this privilege — not a guarded
one, none at all. Privilege escalation into the platform tier is removed as
a category rather than defended against with a role check somebody could
later get wrong.

The cost is this: it runs as the table OWNER, using
`MIGRATIONS_DATABASE_URL`, which means an operator with database access and
a shell. That is the intended bar for minting an account that can change
every tenant's entitlements. It is also why this lives in `scripts/` and
not in `app/` — `tests/test_worker_db_roles.py` sweeps `app/` for exactly
this URL and would (correctly) fail the build if it appeared there.

The granted account still cannot sign in to the console until it enrols a
second factor: `/platform/auth/login` refuses any account with
`mfa_activated_at` unset. Enrol via `/platform/auth/mfa/enroll` and
`/platform/auth/mfa/activate`, both password-gated.
"""
import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timezone

import asyncpg

# Imported for its side-effect-free settings only; this script deliberately
# does not import the app's engine (that one connects as app_user, which by
# design cannot write this table).
from app.config import settings


def _dsn() -> str:
    return settings.migrations_database_url.replace("+asyncpg", "")


async def _lookup_user(conn, email: str) -> tuple[uuid.UUID, str | None] | None:
    row = await conn.fetchrow("SELECT id, full_name FROM users WHERE email = $1", email)
    return (row["id"], row["full_name"]) if row else None


async def cmd_list(conn, _args) -> int:
    rows = await conn.fetch(
        """
        SELECT u.email, u.full_name, p.granted_at, p.revoked_at,
               u.mfa_activated_at IS NOT NULL AS mfa_active
        FROM platform_admins p JOIN users u ON u.id = p.user_id
        ORDER BY p.granted_at
        """
    )
    if not rows:
        print("No platform administrators.")
        return 0
    for row in rows:
        state = "REVOKED" if row["revoked_at"] else "active"
        mfa = "MFA on" if row["mfa_active"] else "MFA NOT ENROLLED — cannot sign in"
        print(f"{row['email']:<40} {state:<8} {mfa:<34} granted {row['granted_at']:%Y-%m-%d}")
    return 0


async def cmd_grant(conn, args) -> int:
    found = await _lookup_user(conn, args.email)
    if found is None:
        print(
            f"No user with email {args.email}. The person must already have an "
            "account — this grants a privilege, it does not create a login.",
            file=sys.stderr,
        )
        return 1
    user_id, full_name = found

    granted_by = None
    if args.granted_by:
        granter = await _lookup_user(conn, args.granted_by)
        if granter is None:
            print(f"No user with email {args.granted_by} (--granted-by)", file=sys.stderr)
            return 1
        granted_by = granter[0]

    # Re-granting a revoked admin clears revoked_at rather than erroring:
    # restoring access to someone who previously held it is a normal act.
    await conn.execute(
        """
        INSERT INTO platform_admins (user_id, granted_at, granted_by, revoked_at)
        VALUES ($1, $2, $3, NULL)
        ON CONFLICT (user_id) DO UPDATE
        SET revoked_at = NULL, granted_at = EXCLUDED.granted_at,
            granted_by = EXCLUDED.granted_by
        """,
        user_id,
        datetime.now(timezone.utc),
        granted_by,
    )

    mfa = await conn.fetchval("SELECT mfa_activated_at FROM users WHERE id = $1", user_id)
    print(f"Granted platform admin to {args.email} ({full_name or 'no name'}).")
    if mfa is None:
        print(
            "  NOTE: this account has no active second factor, so it cannot sign in "
            "to the console yet. Enrol via POST /platform/auth/mfa/enroll then "
            "/platform/auth/mfa/activate."
        )
    return 0


async def cmd_revoke(conn, args) -> int:
    found = await _lookup_user(conn, args.email)
    if found is None:
        print(f"No user with email {args.email}", file=sys.stderr)
        return 1

    # Soft revoke: who held this and when is exactly what an incident review
    # asks about, and a DELETE answers it with silence.
    updated = await conn.execute(
        "UPDATE platform_admins SET revoked_at = $2 WHERE user_id = $1 AND revoked_at IS NULL",
        found[0],
        datetime.now(timezone.utc),
    )
    if updated.endswith("0"):
        print(f"{args.email} is not currently a platform admin.", file=sys.stderr)
        return 1
    print(f"Revoked platform admin from {args.email}. Takes effect on their next request.")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show every platform admin, active or revoked")
    grant = sub.add_parser("grant", help="grant the privilege to an existing user")
    grant.add_argument("email")
    grant.add_argument("--granted-by", help="email of the operator authorising this")
    revoke = sub.add_parser("revoke", help="revoke the privilege (soft; keeps the record)")
    revoke.add_argument("email")
    args = parser.parse_args()

    conn = await asyncpg.connect(_dsn())
    try:
        return await {"list": cmd_list, "grant": cmd_grant, "revoke": cmd_revoke}[args.command](
            conn, args
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
