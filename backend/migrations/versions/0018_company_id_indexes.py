"""Add a `company_id` index to every tenant table that was missing one.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-23

Every RLS policy in this codebase filters on
`company_id IN (SELECT id FROM get_all_descendant_ids(...))` (or
`get_root_company_id(...)`) — this filter runs on EVERY query against
EVERY tenant table, not just ones an app author happened to add an index
for. A subset of tables already had a `company_id`-leading index, whether
a dedicated `create_index` call (`leads`, `audit_log`, `invoices`,
`bills`, `compliance_documents`) or a `UniqueConstraint`/`unique=True`
column whose own index incidentally covers `company_id` as its leading
column (`subscriptions`, `integration_connections`, `company_branding`).
Every other tenant table had none at all, forcing a sequential scan to
apply RLS's own filter on tables that will grow without bound in normal
use (tasks, documents, line items, payments) — a correctness-adjacent
perf cliff specific to how this codebase enforces tenant isolation, not a
generic "add indexes for speed" cleanup.

Plain single-column `company_id` indexes, not composite
`(company_id, <other column>)` ones: this migration's scope is closing
the RLS-filter gap itself (every one of these tables was doing a full
scan just to evaluate `company_id IN (...)`), not hand-tuning each
table's own most common list-endpoint query shape — that's a separate,
per-route optimization decision this migration deliberately doesn't make
on any table's behalf. `company_users` and `estimate_line_items`... no,
`estimate_line_items` genuinely has none either (checked directly against
0007's own DDL) — only `company_users` is skipped below, since its
`(company_id, user_id)` composite PRIMARY KEY already gives Postgres an
index with `company_id` as the leading column, same as any other
leading-column-of-a-multicolumn-index case.
"""
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

# (index_name, table_name) — every tenant table that had no company_id-
# leading index of any kind (dedicated, unique constraint, or PK) before
# this migration. Verified directly against each table's own
# create_table/UniqueConstraint DDL in migrations 0001-0013, not assumed.
_MISSING_INDEXES = [
    ("idx_invitations_company_id", "invitations"),
    ("idx_communication_logs_company_id", "communication_logs"),
    ("idx_projects_company_id", "projects"),
    ("idx_phases_company_id", "phases"),
    ("idx_tasks_company_id", "tasks"),
    ("idx_documents_company_id", "documents"),
    ("idx_daily_logs_company_id", "daily_logs"),
    ("idx_markup_profiles_company_id", "markup_profiles"),
    ("idx_cost_catalog_items_company_id", "cost_catalog_items"),
    ("idx_esignatures_company_id", "esignatures"),
    ("idx_estimates_company_id", "estimates"),
    ("idx_estimate_line_items_company_id", "estimate_line_items"),
    ("idx_change_orders_company_id", "change_orders"),
    ("idx_subcontractors_company_id", "subcontractors"),
    ("idx_subcontractor_assignments_company_id", "subcontractor_assignments"),
    ("idx_compliance_notifications_company_id", "compliance_notifications"),
    ("idx_invoice_payments_company_id", "invoice_payments"),
    ("idx_bill_payments_company_id", "bill_payments"),
    ("idx_expenses_company_id", "expenses"),
    ("idx_integration_sync_records_company_id", "integration_sync_records"),
]


def upgrade() -> None:
    for index_name, table_name in _MISSING_INDEXES:
        op.create_index(index_name, table_name, ["company_id"])


def downgrade() -> None:
    for index_name, table_name in _MISSING_INDEXES:
        op.drop_index(index_name, table_name=table_name)
