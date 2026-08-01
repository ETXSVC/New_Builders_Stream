"""Prove the mail configuration works, without inviting a real person.

    python scripts/send_test_email.py you@yourdomain.com

WHY THIS EXISTS. Turning on delivery is one variable (`SMTP_HOST`), and
until now the only way to find out whether it worked was to invite somebody
and ask if the mail arrived. When it did not, there was nothing to look at:
the send happens inside the Dramatiq worker, a relay's rejection surfaces
three retries later as a dropped message, and the invitation row still sits
there looking fine because the email is an optimization on top of it rather
than the only path in (see app/tasks/send_invitation_email.py).

So this sends one message through exactly the client the app would use —
`get_email_client()`, same settings, same STARTTLS and login decisions — and
prints which one that was. With `SMTP_HOST` unset it reports that the
recording fake took the message and nothing left the box, which is the
correct answer for a dev machine rather than an error.

Run it on the server, from the compose stack, so it reads the same
environment the backend does:

    docker compose -f docker-compose.prod.yml exec backend \\
        python scripts/send_test_email.py you@yourdomain.com

The failure modes it turns into one legible line: a wrong host or port
(connection refused / timeout), credentials the relay rejects
(SMTPAuthenticationError), a sender the relay will not accept
(SMTPSenderRefused), and a recipient it will not deliver to
(SMTPRecipientsRefused).
"""
import argparse
import asyncio
import smtplib
import sys

from app.config import settings
from app.services.email import FakeEmailClient, get_email_client


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("recipient", help="where to send the test message")
    parser.add_argument(
        "--subject",
        default="Builders Stream test email",
        help="override the subject, e.g. to tell two attempts apart",
    )
    args = parser.parse_args()

    client = get_email_client()
    using_fake = isinstance(client, FakeEmailClient)

    if using_fake:
        print(
            "SMTP_HOST is not set, so the recording fake is selected — this will "
            "send nothing. Set SMTP_HOST (and SMTP_FROM_ADDRESS) to test real "
            "delivery.",
            file=sys.stderr,
        )
    else:
        print(f"Sending via {settings.smtp_host}:{settings.smtp_port} "
              f"(STARTTLS={settings.smtp_starttls}, "
              f"auth={'yes' if settings.smtp_username else 'no'}) "
              f"from {settings.smtp_from_address}")

    body = (
        "This is a test message from Builders Stream.\n\n"
        "If you are reading it in your inbox, outbound email is configured "
        "correctly: invitations, signature requests and compliance-expiry "
        "notices will reach their recipients the same way.\n"
    )

    try:
        await client.send(to=args.recipient, subject=args.subject, body=body)
    except smtplib.SMTPException as err:
        # Named separately from the catch-all below because these are the
        # answers an operator can act on: the relay said no, and why.
        print(f"The mail server refused the message: {err}", file=sys.stderr)
        return 1
    except OSError as err:
        print(
            f"Could not reach {settings.smtp_host}:{settings.smtp_port} — {err}",
            file=sys.stderr,
        )
        return 1

    if using_fake:
        print(f"Recorded (not sent) for {args.recipient}.")
    else:
        print(
            f"Handed to the mail server for {args.recipient}. "
            "Accepted by the relay is not the same as delivered — check the inbox, "
            "and the spam folder."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
