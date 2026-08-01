"""Tells a subcontractor their compliance document is about to expire.

The daily expiry scan wrote `compliance_notifications` rows and nothing
else — visible only to whoever happened to open the compliance dashboard.
`subcontractors.contact_email` exists precisely so the person who has to
renew the certificate can be told, and nothing used it.

The subcontractor is the recipient, not the company's admins: they are the
only party who can actually act on it, and the admin-facing view of the
same data is the dashboard, which already exists and is not improved by a
daily email. A subcontractor with no `contact_email` on file is skipped —
that is a legitimate state, and the notification row is still written.

No database access here, same as the other notification actors: the scan
passes everything the message needs, so this is safe to retry and cannot
be affected by anything that happens to the scan's transaction afterwards.
"""
import uuid

import dramatiq

from app.services.tenant_smtp import client_for_company
from app.tasks import broker  # noqa: F401 - import-time side effect

# "30_days" -> "30 days". The thresholds are stored in the form the DB
# CHECK constraint pins (app/models/compliance_notification.py); this is
# only about how they read in a sentence.
def _humanize(threshold: str) -> str:
    return threshold.replace("_", " ")


async def _send_compliance_expiry_email(
    *,
    to_email: str,
    subcontractor_name: str,
    company_name: str,
    doc_type: str,
    expires_on: str,
    threshold: str,
    # See send_invitation_email.py: defaulted for in-flight messages,
    # resolved by the daily sweep that enqueues this.
    from_name: str | None = None,
    # Which company's mail server to send through (migration 0029).
    # An id, never credentials: a Dramatiq payload lives in Redis and shows
    # up in dead-letter inspection, and another company's mail password has
    # no business being in either. Defaulted for messages enqueued before
    # this existed and still in the queue at deploy time.
    company_id: str | None = None,
) -> None:
    readable_type = doc_type.replace("_", " ")
    client = await client_for_company(uuid.UUID(company_id) if company_id else None)
    await client.send(
        from_name=from_name,
        to=to_email,
        subject=f"Your {readable_type} expires in {_humanize(threshold)}",
        body=(
            f"Hello {subcontractor_name},\n\n"
            f"{company_name} has your {readable_type} on file, and it expires "
            f"on {expires_on} — {_humanize(threshold)} from now.\n\n"
            f"Please send {company_name} an updated copy before then to stay "
            f"eligible for work.\n\n"
            f"If you've already renewed it, you can ignore this message."
        ),
    )


send_compliance_expiry_email = dramatiq.actor(
    max_retries=3, actor_name="send_compliance_expiry_email"
)(_send_compliance_expiry_email)
