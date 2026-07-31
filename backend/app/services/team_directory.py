"""How other modules ask the team directory who somebody is.

`GET /companies/members` is the assignee picker's data source and says so in
its own docstring. Until now it could only offer `users.full_name` — the
name a person set on their own ACCOUNT — so a picker asking "who should do
this?" answered with logins and no trades, while migration 0026's directory
sat next to it holding exactly the two facts that question wants.

This is the sanctioned way across that boundary. `companies.py` must not
`select(MemberProfile)` itself: modules reach another module's data through
its service layer, and `tests/test_module_boundaries.py` makes that a gate
rather than a convention.

**Two fields, not the row.** A dropdown needs a name and a trade. It does
not need an address, and it very much does not need `notes` — the company's
private record ABOUT a person, which `/team` itself withholds from its own
subject. Returning the whole profile here would put both into every picker
payload in the app, which is how a field ends up somewhere nobody meant to
publish it.
"""
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MemberProfile, Profession


@dataclass(frozen=True)
class DirectoryLabel:
    """What this company calls somebody, and what they do."""

    filed_name: str | None
    profession: str | None


async def labels_for_members(
    session: AsyncSession, company_id: uuid.UUID, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, DirectoryLabel]:
    """Directory labels for the members given, keyed by user id.

    Members with no profile row are simply absent from the result rather
    than present-and-empty: a profile is created on first edit, so "not in
    here" is the normal state for somebody who just joined, and the caller
    already has their account name to fall back on.

    Scoped by `company_id` explicitly rather than leaning on RLS alone, for
    the reason every by-id lookup in this codebase gives: a parent-branch
    session can see descendant rows, and a picker must offer the company
    being acted as, not whichever branch matched first.
    """
    if not user_ids:
        return {}

    result = await session.execute(
        select(MemberProfile, Profession.name)
        .outerjoin(Profession, MemberProfile.profession_id == Profession.id)
        .where(
            MemberProfile.company_id == company_id,
            MemberProfile.user_id.in_(user_ids),
        )
    )

    labels: dict[uuid.UUID, DirectoryLabel] = {}
    for profile, profession_name in result.all():
        filed = " ".join(part for part in (profile.first_name, profile.last_name) if part).strip()
        labels[profile.user_id] = DirectoryLabel(
            filed_name=filed or None, profession=profession_name
        )
    return labels
