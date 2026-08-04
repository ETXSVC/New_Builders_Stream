"""A line an estimator writes themselves, for work the catalog does not price.

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-04

Every estimate line has so far had to resolve to a cost catalog item, because
`cost_catalog_item_id` was NOT NULL. That is right for anything the company
prices routinely, and wrong for the last line of most real estimates: site
cleanup, a permit fee, a one-off allowance. The workaround in the field is a
catalog item called "Miscellaneous" priced at $1.00 with the dollar amount
typed into the quantity box, which prints "400 x $1.00" on the document a
customer signs.

So a line item is now EITHER catalogued OR free-form, and the check
constraint below is what makes that "either/or" rather than "any combination":

* **catalogued** — `cost_catalog_item_id` set, `description`/`unit` NULL
  (both live on the catalog item), rate copied from the catalog as before.
* **free-form** — `cost_catalog_item_id` NULL, `description` and `unit`
  supplied by the estimator, and `unit_rate_snapshot` is **their number**.

**That last part deliberately breaches a rule this schema states outright** —
`EstimateLineItemInput`'s docstring says "an estimator still cannot assert a
price", and for a catalogued line that remains exactly true. The rule exists
to stop a line silently disagreeing with the catalog it came from, and to stop
a later catalog edit retroactively re-pricing a signed estimate. A free-form
line has no catalog item behind it, so there is no true price for it to
disagree with and nothing upstream that could later move. The guard on
catalogued lines is untouched; this only carves out the case the guard was
never about.

`unit_rate_snapshot` and `line_total` stay NOT NULL for both shapes, so every
consumer that only sums money keeps working. The consumers that JOIN through
`cost_catalog_item_id` do not, and are fixed in the same change — see
`app/services/estimate_calculation.py` and `app/tasks/estimate_pdf.py`, both of
which carried an INNER JOIN justified by a comment asserting this column could
never be NULL.
"""
import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

# Named here and in app/models/estimate_line_item.py so the two cannot drift.
_EXACTLY_ONE_SHAPE = (
    "(cost_catalog_item_id IS NOT NULL AND description IS NULL AND unit IS NULL)"
    " OR "
    "(cost_catalog_item_id IS NULL AND description IS NOT NULL AND unit IS NOT NULL)"
)


def upgrade() -> None:
    op.alter_column(
        "estimate_line_items",
        "cost_catalog_item_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    # VARCHAR(255)/VARCHAR(50) mirror `cost_catalog_items.name`/`.unit`, the
    # columns these stand in for on a free-form line — a description that is
    # allowed to be longer than any catalog item's name would print
    # differently on the PDF depending on which kind of line it was.
    op.add_column("estimate_line_items", sa.Column("description", sa.String(255), nullable=True))
    op.add_column("estimate_line_items", sa.Column("unit", sa.String(50), nullable=True))

    # The router validates this too. Both, deliberately: the router's version
    # gives a caller a 422 that says which field is wrong, and this one makes
    # the invalid state unrepresentable for anything that writes the table by
    # another path — a migration, a fixture, a future service. Same
    # belt-and-suspenders pattern as `ck_tasks_status` beside
    # `TaskUpdateRequest`'s own validator.
    op.create_check_constraint(
        "ck_estimate_line_items_catalogued_xor_free_form",
        "estimate_line_items",
        _EXACTLY_ONE_SHAPE,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_estimate_line_items_catalogued_xor_free_form",
        "estimate_line_items",
        type_="check",
    )
    # Free-form lines cannot survive a downgrade: there is no catalog item to
    # point them at, and inventing one would put a fabricated row in a
    # company's price list. Deleting them loses data, and that is the honest
    # outcome of reverting a feature whose whole point was storing something
    # the old shape could not represent — a downgrade that instead left the
    # NOT NULL constraint off would leave the schema quietly wrong.
    op.execute("DELETE FROM estimate_line_items WHERE cost_catalog_item_id IS NULL")
    op.drop_column("estimate_line_items", "unit")
    op.drop_column("estimate_line_items", "description")
    op.alter_column(
        "estimate_line_items",
        "cost_catalog_item_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=False,
    )
