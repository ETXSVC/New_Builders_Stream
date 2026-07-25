"""Get the background jobs off the Postgres superuser, and the runtime
passwords out of this repository.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-25

Two related problems, both about which role holds which privilege.

1. ALL FOUR BACKGROUND JOBS RAN AS THE TABLE OWNER

`app/tasks/{accounting_sync,compliance_expiry,seat_usage,
flag_overdue_financial_records}.py` each built an engine from
`settings.migrations_database_url` — the `postgres` role that owns every
table. That role is exempt from RLS *and* can DROP tables, ALTER policies
and disable row security outright. So the entire async job layer ran with
the highest privilege in the database, and its tenant isolation rested
entirely on hand-written `WHERE company_id = ...` clauses with no safety
net: one missing filter is a silent cross-tenant leak, and it directly
contradicts CLAUDE.md's own "never reach for an owner-role connection in
application code."

The two shapes of job get different fixes, because they have genuinely
different needs:

  * **Single-tenant jobs** (`accounting_sync` — it is handed one
    connection id and works on that company alone) move to the ordinary
    RLS-constrained `app_user`, resolving their tenant first and then
    running under `set_current_tenant`. RLS becomes a real backstop rather
    than a bypassed one. `get_integration_connection_company_id` below is
    what makes that possible: reading the connection row to *learn* the
    tenant is the chicken-and-egg problem RLS creates for a job with no
    caller, and this is the same narrow SECURITY DEFINER escape migration
    0011 already established for the Stripe webhook.

  * **Genuinely cross-tenant scans** (the three daily sweeps) move to a new
    `scanner` role: LOGIN + BYPASSRLS, granted only DML on existing tables.
    It still sees every tenant — that is the job — but it owns nothing, so
    it cannot alter a policy, disable row security, or drop a table. That
    is the meaningful reduction available without turning one daily scan
    into N per-tenant transactions.

`scanner` is deliberately a separate role rather than BYPASSRLS on
`app_user`: granting it to `app_user` would silently void every RLS policy
for the entire request path, which is the one thing this schema's design
rests on (and which `tests/test_rls_policy_coverage.py` asserts against).

2. THE RUNTIME PASSWORD WAS A LITERAL IN MIGRATION 0001

`CREATE ROLE app_user WITH LOGIN PASSWORD 'app_password'` — a publicly
known password for the role the application authenticates as, created by
running `alembic upgrade head` on any box including production.

Both roles' passwords now come from the environment (`APP_DB_PASSWORD` /
`SCANNER_DB_PASSWORD`) when set. Left unset, the dev/test defaults are kept
so a local `docker compose up` and the test suite keep working unchanged.
That is safe rather than lax because `app/config.py`'s production validator
already refuses to boot on a `DATABASE_URL` containing `:app_password@` —
forgetting to set them in production is a loud startup failure, not a
silent weak credential.
"""
import os

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

# Matches migration 0001's literal, so an operator who sets nothing gets
# exactly today's behavior.
_DEFAULT_APP_PASSWORD = "app_password"
_DEFAULT_SCANNER_PASSWORD = "scanner_password"


def _quote_literal(value: str) -> str:
    """Postgres string literal. Passwords come from the environment, so they
    are trusted input, but doubling quotes costs nothing and keeps a
    password containing an apostrophe from producing a syntax error at
    deploy time."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def upgrade() -> None:
    app_password = os.environ.get("APP_DB_PASSWORD", _DEFAULT_APP_PASSWORD)
    scanner_password = os.environ.get("SCANNER_DB_PASSWORD", _DEFAULT_SCANNER_PASSWORD)

    # Rotate app_user off migration 0001's committed literal whenever the
    # environment supplies a real one. Unconditional ALTER (not "only if it
    # differs") so re-running with a rotated value is the supported way to
    # change the password.
    op.execute(f"ALTER ROLE app_user WITH PASSWORD {_quote_literal(app_password)}")

    # --- scanner: cross-tenant reads, zero ownership --------------------
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'scanner') THEN
                CREATE ROLE scanner WITH LOGIN BYPASSRLS PASSWORD {_quote_literal(scanner_password)};
            ELSE
                ALTER ROLE scanner WITH LOGIN BYPASSRLS PASSWORD {_quote_literal(scanner_password)};
            END IF;
        END
        $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO scanner")
    # DML only. No CREATE on the schema, no ownership of anything: scanner
    # cannot add a table, drop one, or touch a policy.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO scanner")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO scanner"
    )
    op.execute("GRANT EXECUTE ON FUNCTION get_all_descendant_ids(UUID) TO scanner")
    op.execute("GRANT EXECUTE ON FUNCTION get_root_company_id(UUID) TO scanner")

    # --- tenant lookup for the single-tenant job ------------------------
    #
    # Same shape and same justification as migration 0011's
    # get_subscription_company_id: a job holding only an
    # integration_connection id has to read that row to discover which
    # tenant it belongs to, but the RLS policy on that table needs the
    # tenant to already be set. SECURITY DEFINER with a pinned search_path,
    # returning exactly one column for exactly one row, EXECUTE revoked
    # from PUBLIC — the narrowest possible escape, not a general-purpose
    # bypass.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION get_integration_connection_company_id(connection_id UUID)
        RETURNS UUID
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT company_id FROM integration_connections WHERE id = connection_id
        $$
        """
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION get_integration_connection_company_id(UUID) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION get_integration_connection_company_id(UUID) TO app_user"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS get_integration_connection_company_id(UUID)")

    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM scanner"
    )
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM scanner")
    op.execute("REVOKE ALL ON SCHEMA public FROM scanner")
    op.execute("REVOKE EXECUTE ON FUNCTION get_all_descendant_ids(UUID) FROM scanner")
    op.execute("REVOKE EXECUTE ON FUNCTION get_root_company_id(UUID) FROM scanner")
    op.execute("DROP ROLE IF EXISTS scanner")

    op.execute(f"ALTER ROLE app_user WITH PASSWORD {_quote_literal(_DEFAULT_APP_PASSWORD)}")
