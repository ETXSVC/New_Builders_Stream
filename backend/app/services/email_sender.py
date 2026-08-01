"""Whose name outbound email goes out under.

One rule, in one place, because four enqueue sites need it and each of them
already knows the company's name: `POST /invitations`, the two
send-for-signature routes, and the daily compliance-expiry sweep. A rule
copied four times is a rule that will read differently in at least one of
them within a year.

**Empty means the company's own name.** `company_branding.email_sender_name`
(migration 0027) is `NOT NULL DEFAULT ''`, so every tenant already has a
sensible display name — the name on their company record — without anyone
typing one, and a company that wants something else says so.

Resolved when the mail is ENQUEUED, not when it is sent. The actors that do
the sending deliberately have no database access (see
`app/tasks/send_invitation_email.py`), and the alternative — a worker
re-reading branding at send time — would re-introduce exactly the
enqueued-before-commit race that design avoids. It also means the name is
the one that was configured when the thing happened, which is the honest
answer for a record of an event.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CompanyBranding


async def sender_name_for(
    session: AsyncSession, company_id: uuid.UUID, company_name: str
) -> str:
    """The display name for `company_id`'s outbound mail.

    Falls back to `company_name` when the tenant has set nothing — and also
    when there is no branding row at all, which is the state every company
    starts in (the row is created on first edit, like the team profile).
    """
    configured = await session.scalar(
        select(CompanyBranding.email_sender_name).where(
            CompanyBranding.company_id == company_id
        )
    )
    return (configured or "").strip() or company_name
