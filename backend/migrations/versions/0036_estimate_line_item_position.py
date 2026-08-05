"""An estimate's lines keep the order the estimator put them in.

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-04

Every read of `estimate_line_items` ordered by `id` — the API's estimate
detail, the recalculate response, and `app/tasks/estimate_pdf.py`, which
renders the document a customer signs. `id` is a random `uuid4`
(`app/models/base.py`'s `new_uuid`), so "ordered by id" is **arbitrary
order**, stable for a given set of rows and unrelated to anything a person
did. An estimator arranges lines the way the job runs — demolition, framing,
finishes, then the allowances — saves, and gets them back shuffled. On the
PDF that is the quote a customer reads.

`position` records the arrangement explicitly. The contract is the obvious
one and needs no new API field: **the order the lines are sent in is the
order they come back in**, so the array index IS the position. `Phase.sequence`
is the same idea already in this schema.

## Why not order by `created_at`

`estimate_line_items` has no timestamp columns at all (deliberately — see the
model), so this would mean adding one. It would also be the wrong column:
`PUT /estimates/{id}/lines` is a batch replace that DELETEs every row and
re-INSERTs, so after any edit "creation order" is just the order the client
happened to serialise. That is the same fact `position` records, but recorded
by accident, unenforceably, in a column whose values are set by a Python
default within one flush and can therefore tie. Ordering on a tie-prone
timestamp is the instability this codebase already rejected for cursor
pagination (`app/core/pagination.py`).

## The unique constraint is not decoration

`uq_estimate_line_items_estimate_position` makes two lines claiming one slot
unrepresentable, in the same spirit as 0035's check constraint. It also closes
a **pre-existing** corruption that has nothing to do with ordering.

Two concurrent `PUT .../lines` on one estimate today: T2's `DELETE ... WHERE
estimate_id = X` runs against its own statement snapshot, so it does not see
the rows T1 is about to commit. T2 deletes what it can see, then inserts its
own lines — and the estimate ends up holding **both** sets, with a total
roughly double the true one, silently. With this constraint the second writer
gets an integrity error instead of writing a corrupted estimate. A 500 on a
genuine concurrent edit is a bad error and a much better outcome than a quote
that is quietly wrong by 2x; turning it into a 409 is worth doing separately.

## Backfill

`row_number() OVER (PARTITION BY estimate_id ORDER BY id)` — i.e. exactly the
order those rows are being displayed in today. Existing estimates therefore
look identical after this migration rather than being re-shuffled once more on
the way to being stable. There is no better source: the information about what
order a person intended was never recorded, which is the whole point.
"""
import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Added nullable, backfilled, then made NOT NULL — the standard three-step,
    # because the table has rows in every deployment this runs against and a
    # NOT NULL column with no server default cannot be added to a populated
    # table in one statement.
    op.add_column("estimate_line_items", sa.Column("position", sa.Integer(), nullable=True))

    # 0-based, matching the request array's own indices, so the router can
    # write `enumerate(...)` without an off-by-one to remember.
    op.execute(
        """
        UPDATE estimate_line_items AS eli
        SET position = ordered.rn - 1
        FROM (
            SELECT id, row_number() OVER (PARTITION BY estimate_id ORDER BY id) AS rn
            FROM estimate_line_items
        ) AS ordered
        WHERE eli.id = ordered.id
        """
    )

    op.alter_column("estimate_line_items", "position", existing_type=sa.Integer(), nullable=False)

    op.create_unique_constraint(
        "uq_estimate_line_items_estimate_position",
        "estimate_line_items",
        ["estimate_id", "position"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_estimate_line_items_estimate_position",
        "estimate_line_items",
        type_="unique",
    )
    op.drop_column("estimate_line_items", "position")
