"""A tenant's own mail server: where its credentials live, and what a
tenant is not allowed to point us at.

Until now every message left through one platform relay. A builder who
wants mail to come from their own domain — which is what stops it looking
like a third party writing on their behalf, and what SPF/DKIM can actually
be published for — needs their own SMTP server configured, and that means
this application storing somebody else's mail credentials and connecting
wherever they say.

Both halves of that sentence are a hazard, and this module is where each
one is answered.

## The credentials

Encrypted at rest with the same Fernet key the integrations module uses
for OAuth tokens (`app/services/token_encryption.py`), and **never
returned by any route** — the API answers `has_password`, not the
password. They are decrypted in one place, in the worker, at the moment of
sending, and they are deliberately NOT put into the Dramatiq payload: a
message body sits in Redis, appears in dead-letter inspection, and would
put another company's mail password somewhere nobody expects to find one.
That is why `send_*_email` actors take a `company_id` and resolve here,
rather than being handed a ready-made client.

## The destination

**A tenant-supplied hostname is an outbound request we make on their
behalf**, which is the definition of SSRF. Left unguarded, a customer
could point `smtp_host` at `127.0.0.1`, at `169.254.169.254` (cloud
metadata), or at a machine inside our network, and the "test connection"
button would report back what it found — a port scanner with a UI.

`resolve_and_check` therefore resolves the name and refuses any address
that is loopback, private, link-local, multicast or otherwise not a
public unicast address. It checks the RESOLVED addresses, not the string:
a hostname the tenant controls can point at 127.0.0.1 just as easily, and
checking the text would catch neither that nor a DNS entry changed after
it was saved. The resolution happens on every send for the same reason.

What this does not defend against is a public address that happens to be
somebody else's mail server, because that is indistinguishable from the
legitimate case — a tenant naming their own provider.
"""
import ipaddress
import socket
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CompanyEmailSettings
from app.services.token_encryption import TokenDecryptionError, decrypt_token


class UnsafeMailHostError(Exception):
    """The configured host resolves somewhere a tenant may not send us."""


@dataclass(frozen=True)
class SmtpConfig:
    """One SMTP endpoint, resolved and ready to use.

    A value object rather than a settings read, so the same client code
    serves the platform relay and a tenant's own server without one of
    them being a special case.
    """

    host: str
    port: int
    from_address: str
    starttls: bool
    username: str | None = None
    password: str | None = None
    # Whether the OPERATOR chose this host, rather than a tenant.
    #
    # The SSRF guard exists because a tenant naming a host makes us connect
    # somewhere on their say-so. The platform's own `SMTP_HOST` is the
    # opposite: it comes from the deployment's environment, and pointing it
    # at a relay on a private network is a normal self-hosted setup — the
    # same one `config.py` already blesses by allowing an unauthenticated
    # relay. Applying the guard to it would refuse a valid configuration
    # and break every send.
    operator_configured: bool = False


def _addresses_for(host: str, port: int) -> list[str]:
    """Every address `host` resolves to.

    A named function rather than an inline `socket.getaddrinfo` so tests
    can replace THIS and nothing else. Patching `socket.getaddrinfo`
    globally also re-points the database driver's own lookups, which fails
    in a way that looks nothing like DNS (`unexpected connection_lost()`).
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as err:
        raise UnsafeMailHostError(f"{host} could not be resolved") from err
    # `info[4]` is the sockaddr: (host, port) for IPv4 and a 4-tuple for
    # IPv6, so the element type is `str | int` as far as the checker knows.
    # The first element is the address in both cases.
    return [str(info[4][0]) for info in infos]


def resolve_and_check(host: str, port: int) -> None:
    """Refuse a host that resolves anywhere private.

    Raises `UnsafeMailHostError` with a message safe to show the tenant —
    it names what was refused, never what was found, so this cannot be
    used to map a network by reading error text.
    """
    for raw in _addresses_for(host, port):
        address = ipaddress.ip_address(raw)
        if not address.is_global or address.is_multicast:
            raise UnsafeMailHostError(
                f"{host} resolves to an address on a private or reserved network, "
                "which this platform will not connect to. Use your mail provider's "
                "public hostname."
            )


async def load_for_company(
    session: AsyncSession, company_id: uuid.UUID
) -> SmtpConfig | None:
    """This company's own mail server, or None to use the platform's.

    None is the ordinary state: a company that has configured nothing, or
    has configured something and turned it off. The caller falls back
    rather than failing, because a tenant with a broken mail setup should
    still be reachable by the platform relay — losing invitations entirely
    is a worse outcome than sending them from the wrong domain.
    """
    row = (
        await session.execute(
            select(CompanyEmailSettings).where(CompanyEmailSettings.company_id == company_id)
        )
    ).scalar_one_or_none()

    if row is None or not row.enabled:
        return None

    password: str | None = None
    if row.password_encrypted:
        try:
            password = decrypt_token(row.password_encrypted)
        except TokenDecryptionError:
            # A key rotation that left this row behind. Falling back to the
            # platform relay beats failing the send: the tenant's mail keeps
            # flowing while somebody re-enters the password.
            return None

    return SmtpConfig(
        host=row.host,
        port=row.port,
        from_address=row.from_address,
        starttls=row.starttls,
        username=row.username or None,
        password=password,
    )


async def client_for_company(company_id: uuid.UUID | None):
    """The email client a message for `company_id` should go through.

    Called from the worker, which is why it opens its own session: the
    alternative is putting a mail password in a Dramatiq payload, and a
    payload lives in Redis and shows up in dead-letter inspection. This is
    the reason the `send_*_email` actors bend their otherwise strict
    no-database rule — they carry a company id, not credentials.

    Runs as `app_user` with `set_current_tenant`, the same shape
    `app/tasks/accounting_sync.py` documents for a single-tenant job: the
    company id here comes from our own enqueue, never from a caller, so
    scoping to it is a narrowing rather than a trust decision.

    Falls back to the platform client for a company with nothing
    configured, for a null id (a message enqueued before this existed and
    still in Redis), and for any failure reading the settings — mail
    leaving through the platform relay beats mail not leaving.
    """
    # Imported here, not at module scope: app.services.email imports THIS
    # module for SmtpConfig, and the pair would be a cycle.
    from app.services.email import SmtpEmailClient, get_email_client

    if company_id is None:
        return get_email_client()

    from app.db import SessionLocal, set_current_tenant

    async with SessionLocal() as session:
        await set_current_tenant(session, str(company_id))
        config = await load_for_company(session, company_id)

    if config is None:
        return get_email_client()
    return SmtpEmailClient(config)
