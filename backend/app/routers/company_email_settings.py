"""GET/PUT/DELETE /companies/email-settings and its test button
(migration 0029).

A company that has published SPF/DKIM for their own domain can put their
own mail server here, and everything this platform sends on their behalf
leaves through it. Admin only, on both sides: these are mail credentials
and a sending identity, which is narrower even than branding's
admin-write/PM-read split — a project manager has no reason to read the
username somebody's mail provider issued.

**The password goes in and never comes out.** Stored as a Fernet
ciphertext, returned as `has_password`, and decrypted in exactly one place
at the moment of sending (`app/services/tenant_smtp.py`).

**A host is checked before it is stored and again before every send.** A
tenant naming a host is asking us to make an outbound connection on their
behalf, and that is SSRF unless somebody says which destinations are
allowed. The whole argument, and what it deliberately does not defend
against, is in `tenant_smtp.py`.
"""
import smtplib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.models import CompanyEmailSettings
from app.schemas.company_email_settings import (
    CompanyEmailSettingsPutRequest,
    CompanyEmailSettingsResponse,
    EmailSettingsTestResponse,
)
from app.services.audit import write_audit_log
from app.services.email import SmtpEmailClient
from app.services.email_sender import sender_name_for
from app.services.tenant_smtp import UnsafeMailHostError, load_for_company, resolve_and_check
from app.services.token_encryption import encrypt_token

router = APIRouter(prefix="/companies/email-settings", tags=["email-settings"])

_ADMIN = ("admin",)


def _to_response(row: CompanyEmailSettings) -> CompanyEmailSettingsResponse:
    return CompanyEmailSettingsResponse(
        host=row.host,
        port=row.port,
        username=row.username,
        from_address=row.from_address,
        starttls=row.starttls,
        enabled=row.enabled,
        has_password=bool(row.password_encrypted),
        verified_at=row.verified_at.isoformat() if row.verified_at else None,
    )


async def _row_for(current: CurrentUser) -> CompanyEmailSettings | None:
    return (
        await current.session.execute(
            select(CompanyEmailSettings).where(
                CompanyEmailSettings.company_id == current.company_id
            )
        )
    ).scalar_one_or_none()


@router.get("", response_model=CompanyEmailSettingsResponse | None)
async def get_email_settings(
    current: CurrentUser = Depends(require_role(*_ADMIN)),
) -> CompanyEmailSettingsResponse | None:
    """The company's mail server, or null if they use the platform's.

    Null rather than a 404: "not configured" is the ordinary state for
    almost every tenant, and a screen asking about it is not making a
    mistake.
    """
    row = await _row_for(current)
    return _to_response(row) if row is not None else None


@router.put("", response_model=CompanyEmailSettingsResponse)
async def put_email_settings(
    payload: CompanyEmailSettingsPutRequest,
    current: CurrentUser = Depends(require_role(*_ADMIN)),
    _ro: None = Depends(block_if_read_only),
) -> CompanyEmailSettingsResponse:
    """Save a mail server. Saving does not prove it works — that is the
    test route below, and `verified_at` is cleared here so the screen
    cannot go on claiming a previous success for a changed host."""
    try:
        resolve_and_check(payload.host, payload.port)
    except UnsafeMailHostError as err:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(err)) from err

    row = await _row_for(current)
    if row is None:
        row = CompanyEmailSettings(company_id=current.company_id, host="", from_address="")
        current.session.add(row)

    row.host = payload.host
    row.port = payload.port
    row.username = payload.username or None
    row.from_address = payload.from_address
    row.starttls = payload.starttls
    row.enabled = payload.enabled

    # Absent means "keep what is stored"; an empty string means "remove
    # it". A form that cannot show the current password would otherwise be
    # able only to clear it.
    if payload.password is not None:
        row.password_encrypted = encrypt_token(payload.password) if payload.password else None

    row.verified_at = None
    await current.session.flush()

    await write_audit_log(
        current.session,
        company_id=current.company_id,
        actor_id=current.user.id,
        action="company.email_settings_updated",
        entity_type="company_email_settings",
        entity_id=row.id,
        # The host and whether credentials exist, never the credentials.
        metadata={"host": row.host, "port": row.port, "enabled": row.enabled},
    )

    return _to_response(row)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_email_settings(
    current: CurrentUser = Depends(require_role(*_ADMIN)),
    _ro: None = Depends(block_if_read_only),
) -> None:
    """Go back to the platform's relay, and forget the credentials.

    Distinct from `enabled=false`, which keeps them for an outage that is
    expected to end.
    """
    row = await _row_for(current)
    if row is None:
        return
    await current.session.delete(row)
    await write_audit_log(
        current.session,
        company_id=current.company_id,
        actor_id=current.user.id,
        action="company.email_settings_removed",
        entity_type="company_email_settings",
        entity_id=row.id,
    )


@router.post("/test", response_model=EmailSettingsTestResponse)
async def test_email_settings(
    current: CurrentUser = Depends(require_role(*_ADMIN)),
    _ro: None = Depends(block_if_read_only),
) -> EmailSettingsTestResponse:
    """Send one message to the admin doing the asking.

    To THEMSELVES, not to an address they type: an endpoint that mails
    arbitrary text to arbitrary recipients through our platform is an open
    relay with extra steps, and "did it arrive" is only answerable by
    somebody who can read the mailbox anyway.

    Answers 200 with `ok: false` rather than an error status, because a
    refused login is a normal outcome of testing a configuration — the
    relay's own words are more useful to the person fixing it than an HTTP
    code.
    """
    config = await load_for_company(current.session, current.company_id)
    if config is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No mail server is configured for this company, or it is turned off.",
        )

    row = await _row_for(current)
    from_name = await sender_name_for(current.session, current.company_id, "Builders Stream")

    try:
        await SmtpEmailClient(config).send(
            to=current.user.email,
            subject="Builders Stream mail server test",
            body=(
                "This is a test message sent through your company's own mail server.\n\n"
                "If you are reading it, invitations, signature requests, expiry notices "
                "and password resets will reach their recipients the same way.\n"
            ),
            from_name=from_name,
        )
    except UnsafeMailHostError as err:
        return EmailSettingsTestResponse(ok=False, detail=str(err))
    except smtplib.SMTPException as err:
        return EmailSettingsTestResponse(ok=False, detail=f"The mail server refused it: {err}")
    except OSError as err:
        return EmailSettingsTestResponse(
            ok=False, detail=f"Could not reach {config.host}:{config.port} — {err}"
        )

    if row is not None:
        row.verified_at = datetime.now(timezone.utc)
        await current.session.flush()

    return EmailSettingsTestResponse(
        ok=True,
        detail=f"Sent to {current.user.email}. Accepted by the server is not the same as "
        "delivered — check the inbox, and the spam folder.",
    )
