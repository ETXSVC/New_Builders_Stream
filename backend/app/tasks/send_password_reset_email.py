"""Sends the password-reset link.

Same undecorated-function/decorated-actor split as every other Dramatiq
actor here, and the same no-database-access rule as
`send_invitation_email.py`: everything the message needs is in the payload,
resolved by the route while it still has a session.

WHAT THE BODY DOES NOT SAY. There is no "somebody requested a reset for
this account" wording that implies the address is registered, because this
mail only ever goes to an address that IS registered — the route answers
identically either way and simply enqueues nothing when nobody has that
address. The "if you did not ask for this" line is the standard reassurance
for the case where somebody else typed your address into the form.

Delivery failures raise and let Dramatiq's max_retries=3 handle it. Unlike
the invitation, there is no in-product fallback if the mail never arrives:
an admin cannot see or resend this link (nothing stores the secret), so the
user simply asks again. That is the deliberate trade for not keeping a
redeemable credential anywhere an operator could read it.
"""
import dramatiq

from app.services import email as email_service
from app.tasks import broker  # noqa: F401 - import-time side effect


async def _send_password_reset_email(
    *,
    to_email: str,
    company_name: str,
    reset_url: str,
    expires_in_minutes: int,
    from_name: str | None = None,
) -> None:
    client = email_service.get_email_client()
    await client.send(
        from_name=from_name,
        to=to_email,
        subject="Reset your Builders Stream password",
        body=(
            f"Somebody asked to reset the password for this address on "
            f"{company_name}'s Builders Stream account.\n\n"
            f"Set a new password here:\n{reset_url}\n\n"
            f"The link works once and expires in {expires_in_minutes} minutes.\n\n"
            f"If you didn't ask for this, you can ignore this email — your "
            f"password has not changed, and the link expires on its own."
        ),
    )


send_password_reset_email = dramatiq.actor(
    max_retries=3, actor_name="send_password_reset_email"
)(_send_password_reset_email)
