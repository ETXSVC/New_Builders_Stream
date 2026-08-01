"""Tells the client an estimate or change order is waiting for their
signature.

`POST /estimates/{id}/send-for-signature` flipped a status column and
notified nobody. The route's name promises otherwise, and the whole
client-facing arc — review, approve, e-sign — starts with the customer
learning there is something to sign. Without this, the only way a client
found out was if someone told them out of band, which is also why nothing
in the product ever linked them to the document.

Same no-database-access shape as `send_invitation_email`, for the same
reason spelled out in that module: the enqueuing route passes everything
the email needs, so a request that rolls back after enqueue costs at worst
one stray email pointing at a document the recipient cannot open. The
alternative — re-reading the estimate here — would inherit the
enqueued-before-commit race `accounting_sync.py` documents, for no gain.

One message per recipient rather than one with several addresses: these go
to a company's customers, who have no business seeing each other's
addresses in a To: header. That is the same reasoning behind the client
scoping in migration 0019, applied to the email layer.
"""
import uuid

import dramatiq

from app.services.tenant_smtp import client_for_company
from app.tasks import broker  # noqa: F401 - import-time side effect

_DOCUMENT_LABELS = {"estimate": "estimate", "change_order": "change order"}


async def _send_signature_request_email(
    *,
    to_email: str,
    company_name: str,
    document_type: str,
    document_url: str,
    # See send_invitation_email.py: defaulted for in-flight messages,
    # resolved by the enqueuing route.
    from_name: str | None = None,
    # Which company's mail server to send through (migration 0029).
    # An id, never credentials: a Dramatiq payload lives in Redis and shows
    # up in dead-letter inspection, and another company's mail password has
    # no business being in either. Defaulted for messages enqueued before
    # this existed and still in the queue at deploy time.
    company_id: str | None = None,
) -> None:
    label = _DOCUMENT_LABELS.get(document_type, "document")
    client = await client_for_company(uuid.UUID(company_id) if company_id else None)
    await client.send(
        from_name=from_name,
        to=to_email,
        subject=f"{company_name} sent you {'an' if label[0] in 'aeiou' else 'a'} {label} to sign",
        body=(
            f"{company_name} has sent you {'an' if label[0] in 'aeiou' else 'a'} "
            f"{label} to review and sign.\n\n"
            f"Open it here:\n{document_url}\n\n"
            f"You'll need to sign in with the account {to_email} to view and "
            f"approve it. If you weren't expecting this, you can ignore this "
            f"email — nothing is agreed until you sign."
        ),
    )


send_signature_request_email = dramatiq.actor(
    max_retries=3, actor_name="send_signature_request_email"
)(_send_signature_request_email)
