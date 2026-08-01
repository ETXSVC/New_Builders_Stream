"""Company branding: the name outbound email goes out under.

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-01

Every email this platform sends — an invitation, a signature request, an
expiring-certificate notice — went out under one address for every tenant,
with no display name at all. The recipient saw `no-reply@…` and had to read
the subject line to work out which builder was writing to them.

This is the name that sits in front of that address: `Riverside Builders
<no-reply@buildersstream.com>`. It belongs beside the logo, the accent
colour and the PDF footer, because it is the same decision — what this
company looks like to somebody outside it — and `company_branding` is
already the tenant-scoped, admin-owned table holding those.

EMPTY MEANS "USE THE COMPANY'S NAME", which is why this is `NOT NULL
DEFAULT ''` rather than nullable. Every existing tenant therefore starts
with a sensible display name the moment this ships, without a data
migration inventing one, and a company that wants something else
("Riverside Scheduling") types it. Nullable would have made "unset" and
"deliberately blank" indistinguishable for a field where blank is not a
thing anyone wants to send.

The ADDRESS is deliberately not here. Sending as `@thatbuilder.com` needs
that builder's DNS (SPF/DKIM) or the mail is spam at best and rejected at
worst — a per-tenant mail server is a much larger piece of work than a
display name, and mixing the two would ship the half that silently fails
to deliver.
"""
import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "company_branding",
        sa.Column(
            "email_sender_name",
            sa.String(120),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("company_branding", "email_sender_name")
