"""Regression sweep for migration 0018 (`company_id` index coverage).

Every RLS policy in this codebase filters on `company_id IN (SELECT id FROM
get_all_descendant_ids(...))` — a filter Postgres has to evaluate on every
query against every tenant table. Before migration 0018, most tenant
tables had no index with `company_id` as a leading column at all, forcing
a sequential scan just to apply RLS's own filter (see that migration's own
docstring for the full audit-finding writeup this closes).

This test doesn't special-case the 20 tables that migration added indexes
for — it queries Postgres's own catalogs for EVERY table with a
`company_id` column and asserts each one has SOME index with `company_id`
as its leading key column, `company_users` (whose composite
`(company_id, user_id)` PRIMARY KEY already covers it) included by that
same general rule rather than as a named exception. This is deliberately
a forward-looking regression guard, not just a one-time assertion about
migration 0018's own table list: a FUTURE tenant table added without a
`company_id` index fails this test too, the same class of gap the
underlying audit finding surfaced.
"""
import asyncpg

from tests.conftest import TEST_DATABASE_URL

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")

_UNINDEXED_COMPANY_ID_TABLES_QUERY = """
    SELECT t.relname AS table_name
    FROM pg_class t
    JOIN pg_namespace n ON n.oid = t.relnamespace AND n.nspname = 'public'
    WHERE t.relkind = 'r'
      AND EXISTS (
        SELECT 1 FROM pg_attribute a
        WHERE a.attrelid = t.oid AND a.attname = 'company_id' AND NOT a.attisdropped
      )
      AND NOT EXISTS (
        SELECT 1
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
        WHERE i.indrelid = t.oid AND a.attname = 'company_id'
      )
    ORDER BY t.relname
"""


async def test_every_table_with_a_company_id_column_has_a_leading_index_on_it():
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        rows = await conn.fetch(_UNINDEXED_COMPANY_ID_TABLES_QUERY)
    finally:
        await conn.close()

    unindexed = [row["table_name"] for row in rows]
    assert unindexed == [], (
        "these tables have a company_id column but no index (dedicated, "
        "unique constraint, or primary key) with company_id as its "
        f"leading column, forcing every RLS-filtered query against them "
        f"to sequential-scan: {unindexed!r}"
    )
