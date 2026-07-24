"""Sends the invitation email carrying the accept-page link.

Same undecorated-function/decorated-actor split as every other Dramatiq
actor in this codebase (see app/tasks/flag_overdue_financial_records.py's
docstring for the rationale). Unlike the other actors this one needs NO
database access at all — the enqueuing route passes everything the email
needs in the message payload, so a request that rolls back after enqueue
costs at worst one stray email pointing at an invitation id that 404s on
the accept page (an explicitly acceptable outcome; the alternative — the
actor re-reading the invitation row — would inherit the same
enqueued-before-commit race accounting_sync.py documents, for no gain).

Imports the app.services.email MODULE and calls
`email_service.get_email_client()` at call time (not `from ... import
get_email_client`) so tests can monkeypatch the module attribute — the
exact convention app/tasks/accounting_sync.py documents for its own
accounting-client import.

Delivery failures raise and let Dramatiq's max_retries=3/backoff handle
retries; after that the message is dropped. There is deliberately no
sent/failed bookkeeping table — the admin-facing invitation row (and its
copyable accept link) remains the source of truth, and the email is an
optimization on top of it, not the only path in.
"""
import dramatiq

from app.services import email as email_service
from app.tasks import broker  # noqa: F401 - import-time side effect


async def _send_invitation_email(
    *, to_email: str, company_name: str, role: str, accept_url: str
) -> None:
    client = email_service.get_email_client()
    await client.send(
        to=to_email,
        subject=f"You're invited to join {company_name} on Builders Stream",
        body=(
            f"You've been invited to join {company_name} on Builders Stream "
            f"as a {role.replace('_', ' ')}.\n\n"
            f"Accept the invitation and create your account here:\n{accept_url}\n\n"
            f"This link expires in 7 days. If you weren't expecting this "
            f"invitation, you can ignore this email."
        ),
    )


send_invitation_email = dramatiq.actor(
    max_retries=3, actor_name="send_invitation_email"
)(_send_invitation_email)
