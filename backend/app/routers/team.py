"""The team directory: who is in this company, and what we know about them.

Before this, a company's people were a `company_users` row — an id and a
role. There was no way to record a phone number, an address, a trade or a
photo, and no screen to manage any of it.

WHERE THE DATA LIVES, AND WHY IT MATTERS HERE. The profile is a tenant
table keyed by `(company_id, user_id)`, not a column on `users`. `users`
has no row-level security by design — `get_current_user` reads it before
any tenant context exists — so an address stored there would be visible to
every company that person also belongs to. Migration 0026's docstring has
the full reasoning; the consequence for this file is that every query below
is naturally tenant-scoped and no route needs to filter by company by hand.

PERMISSIONS. Reads are `admin` + `project_manager`, matching
`GET /companies/members`, which this is the richer sibling of — a project
manager picking an assignee should see who exists. Writes are `admin`
only: a directory entry is an HR record, and the role that can invite and
offboard is the role that can edit it.

A MEMBER EDITING THEMSELVES is `/team/me`, not this file's `/{user_id}`
routes with a self check bolted on. Two rules, two surfaces: an admin
writes the company's whole record of a person; a person writes only how to
reach them — name, address, phones, photo. `notes` and `profession_id`
stay with the admin because neither is theirs to state, and the split is
made of a separate request schema rather than an `if`, so it cannot be
undone by accident. `_SELF_ROLES` excludes `client` for the same reason
the directory is called the team: a client is somebody else's customer.
"""
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.core.uploads import read_upload_limited
from app.models import CompanyUser, MemberPhone, MemberProfile, Profession, User
from app.schemas.team import (
    MemberProfileUpdateRequest,
    MemberSelfUpdateRequest,
    PhoneEntry,
    ProfessionCreateRequest,
    ProfessionResponse,
    TeamMemberResponse,
    TeamPage,
)
from app.services.audit import write_audit_log
from app.services.concurrency import guard_stale_write
from app.services.document_storage import (
    MAX_LOGO_SIZE_BYTES,
    UnsupportedLogoError,
    write_member_photo_file,
)

router = APIRouter(prefix="/team", tags=["team"])

_READ_ROLES = ("admin", "project_manager")

# Who has a profile of their own to edit. `client` is deliberately absent:
# a client is somebody else's customer with row-scoped access to a project,
# not one of this company's people, and giving them a directory entry would
# put an external contact in the staff list.
_SELF_ROLES = ("admin", "project_manager", "field_crew", "accountant")

# The fields a person may change about THEMSELVES. Everything a company
# records about somebody that is not in this tuple — `notes` and
# `profession_id` — stays with the admin, because neither is theirs to
# state: one is the company's private record about them, the other is how
# the company classifies them for assignment.
_CONTACT_FIELDS = (
    "first_name",
    "last_name",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
)


async def _membership_or_404(current: CurrentUser, user_id: uuid.UUID) -> CompanyUser:
    """The membership in the caller's ACTIVE tenant.

    Scoped by `company_id == current.company_id` rather than leaning on RLS
    alone, for the reason `companies.py::_get_membership_or_404` spells out:
    a parent-branch admin can see descendant memberships, and "edit this
    person" must mean "in the company I am acting as", never "in whichever
    branch matched first".
    """
    result = await current.session.execute(
        select(CompanyUser).where(
            CompanyUser.user_id == user_id, CompanyUser.company_id == current.company_id
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a member of this company")
    return membership


async def _profile_for(current: CurrentUser, user_id: uuid.UUID) -> MemberProfile | None:
    result = await current.session.execute(
        select(MemberProfile).where(
            MemberProfile.company_id == current.company_id, MemberProfile.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def _profile_or_create(current: CurrentUser, user_id: uuid.UUID) -> MemberProfile:
    """The profile row, made on first edit rather than at accept time.

    Creating it lazily is why the directory has no half-empty rows for
    members nobody has got round to filling in.
    """
    profile = await _profile_for(current, user_id)
    if profile is not None:
        return profile
    # `phones=[]` explicitly: a freshly constructed instance has an UNLOADED
    # collection, so the first read of it would attempt a lazy load —
    # synchronous IO under asyncpg, i.e. MissingGreenlet rather than a slow
    # query. Handing it an empty list marks it loaded.
    profile = MemberProfile(company_id=current.company_id, user_id=user_id, phones=[])
    current.session.add(profile)
    await current.session.flush()
    return profile


def _replace_phones(current: CurrentUser, profile: MemberProfile, entries: list[PhoneEntry]) -> None:
    """The list sent IS the list.

    `delete-orphan` on the relationship turns the clear() into DELETEs;
    assigning a fresh list instead would leave the old rows behind with a
    null parent.
    """
    profile.phones.clear()
    for entry in entries:
        profile.phones.append(
            MemberPhone(company_id=current.company_id, label=entry.label, number=entry.number)
        )


async def _profession_map(current: CurrentUser) -> dict[uuid.UUID, Profession]:
    result = await current.session.execute(
        select(Profession).where(Profession.company_id == current.company_id)
    )
    return {row.id: row for row in result.scalars().all()}


def _to_response(
    membership: CompanyUser,
    user: User,
    profile: MemberProfile | None,
    professions: dict[uuid.UUID, Profession],
    *,
    include_notes: bool = True,
) -> TeamMemberResponse:
    """One person, assembled from the three places their data lives.

    A member with no profile row yet is a normal state, not an error: the
    row is created on first edit, so somebody who has just accepted an
    invitation appears immediately with only their account details.

    `include_notes=False` is how the `/team/me` routes below answer: `notes`
    is the company's private record ABOUT a person (migration 0026), so
    handing it back to that person would leak it exactly as badly as letting
    them edit it. It is dropped here rather than in the schema so there is
    one response model and one place that decides.
    """
    profession = (
        professions.get(profile.profession_id)
        if profile is not None and profile.profession_id is not None
        else None
    )
    return TeamMemberResponse(
        user_id=user.id,
        email=user.email,
        account_name=user.full_name,
        role=membership.role,
        joined_at=membership.created_at,
        first_name=profile.first_name if profile else None,
        last_name=profile.last_name if profile else None,
        address_line1=profile.address_line1 if profile else None,
        address_line2=profile.address_line2 if profile else None,
        city=profile.city if profile else None,
        state=profile.state if profile else None,
        postal_code=profile.postal_code if profile else None,
        notes=(profile.notes if profile else None) if include_notes else None,
        profession=(
            ProfessionResponse(id=profession.id, name=profession.name) if profession else None
        ),
        phones=(
            [PhoneEntry(label=p.label, number=p.number) for p in profile.phones]
            if profile
            else []
        ),
        has_photo=bool(profile and profile.image_path),
        updated_at=profile.updated_at if profile else None,
    )


@router.get("", response_model=TeamPage)
async def list_team(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> TeamPage:
    """Everyone in the caller's active tenant.

    Paginated over `company_users`, which is the source of truth for
    membership — profiles are joined on afterwards precisely because a
    member without one must still appear.
    """
    # Paginated over the membership alone, with users and profiles fetched
    # for the page afterwards. `paginate` is typed for a single-entity
    # select, and two extra id-keyed lookups per page is cheaper than the
    # alternative anyway — the page is at most MAX_LIMIT rows.
    memberships, next_cursor = await paginate(
        current.session,
        select(CompanyUser).where(CompanyUser.company_id == current.company_id),
        created_at_col=CompanyUser.created_at,
        id_col=CompanyUser.user_id,
        cursor=cursor,
        limit=limit,
    )

    user_ids = [membership.user_id for membership in memberships]
    users: dict[uuid.UUID, User] = {}
    profiles: dict[uuid.UUID, MemberProfile] = {}
    if user_ids:
        user_rows = await current.session.execute(select(User).where(User.id.in_(user_ids)))
        users = {u.id: u for u in user_rows.scalars().all()}
        profile_rows = await current.session.execute(
            select(MemberProfile).where(
                MemberProfile.company_id == current.company_id,
                MemberProfile.user_id.in_(user_ids),
            )
        )
        profiles = {p.user_id: p for p in profile_rows.scalars().all()}

    professions = await _profession_map(current)
    return TeamPage(
        items=[
            _to_response(m, users[m.user_id], profiles.get(m.user_id), professions)
            for m in memberships
            # A membership whose user row vanished is not a state this
            # schema allows (company_users.user_id CASCADEs), but skipping
            # beats a KeyError if it ever does.
            if m.user_id in users
        ],
        next_cursor=next_cursor,
    )


# --- the company-managed profession list ----------------------------------
#
# Declared BEFORE the `/{user_id}` routes below, and that ordering is the
# only thing making `/team/professions` reachable: Starlette matches in
# registration order, so a `/{user_id}` declared first would swallow the
# literal segment and answer 422 on the word "professions". It is the same
# reason `main.py` registers `branding.router` before `companies.router` —
# the alternative is contorting the path (`/professions/all`) to dodge a
# collision that ordering removes.


@router.get("/professions", response_model=list[ProfessionResponse])
async def list_professions(
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> list[ProfessionResponse]:
    result = await current.session.execute(
        select(Profession)
        .where(Profession.company_id == current.company_id)
        .order_by(Profession.name)
    )
    return [ProfessionResponse(id=p.id, name=p.name) for p in result.scalars().all()]


@router.post("/professions", response_model=ProfessionResponse, status_code=status.HTTP_201_CREATED)
async def create_profession(
    payload: ProfessionCreateRequest,
    current: CurrentUser = Depends(require_role("admin")),
    _ro: None = Depends(block_if_read_only),
) -> ProfessionResponse:
    profession = Profession(company_id=current.company_id, name=payload.name.strip())
    current.session.add(profession)
    try:
        await current.session.flush()
    except IntegrityError:
        # The case-insensitive unique index from migration 0026. Caught
        # rather than pre-checked because a pre-check races.
        raise HTTPException(status.HTTP_409_CONFLICT, "That profession already exists")
    return ProfessionResponse(id=profession.id, name=profession.name)


@router.delete("/professions/{profession_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profession(
    profession_id: uuid.UUID,
    current: CurrentUser = Depends(require_role("admin")),
    _ro: None = Depends(block_if_read_only),
) -> None:
    result = await current.session.execute(
        select(Profession).where(
            Profession.id == profession_id, Profession.company_id == current.company_id
        )
    )
    profession = result.scalar_one_or_none()
    if profession is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such profession")
    # Members holding it are set to NULL by the FK (migration 0026), not
    # blocked and not deleted.
    await current.session.delete(profession)


# --- your own record ------------------------------------------------------
#
# Declared before `/{user_id}`, for the same registration-order reason as the
# professions routes above.
#
# A SEPARATE ROUTE RATHER THAN "the same route, if the id is yours". Both
# would work; this one cannot be got wrong later. `MemberSelfUpdateRequest`
# has no `notes` and no `profession_id` and forbids extras, so the self path
# cannot accept a field it should not accept even if somebody adds one to the
# admin's schema and forgets this file exists. The reads are the same story:
# `include_notes=False` means the company's private record about a person is
# not handed to that person, which a shared route would have to remember to
# strip.
#
# It also means the client never needs its own user id to edit itself, which
# is one less thing for a browser to get wrong.


async def _self_context(current: CurrentUser) -> tuple[CompanyUser, User]:
    membership = await _membership_or_404(current, current.user.id)
    return membership, current.user


@router.get("/me", response_model=TeamMemberResponse)
async def get_my_profile(
    current: CurrentUser = Depends(require_role(*_SELF_ROLES)),
) -> TeamMemberResponse:
    """Your own record, minus the part that is not about you but about how
    your employer files you: `notes` is always null here."""
    membership, user = await _self_context(current)
    return _to_response(
        membership,
        user,
        await _profile_for(current, current.user.id),
        await _profession_map(current),
        include_notes=False,
    )


@router.patch("/me", response_model=TeamMemberResponse)
async def update_my_profile(
    payload: MemberSelfUpdateRequest,
    current: CurrentUser = Depends(require_role(*_SELF_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> TeamMemberResponse:
    """Your name, your address, your phone numbers.

    Audited into the same log as an admin's edit, with `self: true`, because
    "who changed this address" is the question the log exists to answer and
    the answer is different in the two cases.
    """
    membership, user = await _self_context(current)

    existing = await _profile_for(current, current.user.id)
    if existing is not None:
        guard_stale_write(existing, payload.expected_updated_at, entity_name="your profile")
    profile = await _profile_or_create(current, current.user.id)

    fields = payload.model_fields_set
    for name in _CONTACT_FIELDS:
        if name in fields:
            setattr(profile, name, getattr(payload, name))

    if payload.phones is not None:
        _replace_phones(current, profile, payload.phones)

    await current.session.flush()

    await write_audit_log(
        current.session,
        company_id=current.company_id,
        actor_id=current.user.id,
        action="team.profile_updated",
        entity_type="member_profile",
        entity_id=profile.id,
        metadata={"user_id": str(current.user.id), "fields": sorted(fields), "self": True},
    )

    return _to_response(
        membership, user, profile, await _profession_map(current), include_notes=False
    )


@router.post("/me/photo", response_model=TeamMemberResponse)
async def upload_my_photo(
    file: UploadFile = File(...),
    current: CurrentUser = Depends(require_role(*_SELF_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> TeamMemberResponse:
    membership, user = await _self_context(current)
    content = await read_upload_limited(file, MAX_LOGO_SIZE_BYTES)
    try:
        relative_path = write_member_photo_file(
            company_id=current.company_id,
            user_id=current.user.id,
            content_type=file.content_type or "",
            content=content,
        )
    except UnsupportedLogoError as err:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(err)) from err

    profile = await _profile_or_create(current, current.user.id)
    profile.image_path = relative_path
    await current.session.flush()

    return _to_response(
        membership, user, profile, await _profession_map(current), include_notes=False
    )


@router.get("/me/photo")
async def get_my_photo(
    current: CurrentUser = Depends(require_role(*_SELF_ROLES)),
) -> FileResponse:
    """Your own photo.

    A route of its own rather than `/team/{your id}/photo`, because that one
    is behind the directory's read roles — and field crew, who can edit
    their own record, cannot open the directory at all.
    """
    return _photo_response(await _profile_for(current, current.user.id))


# --- one person -----------------------------------------------------------


@router.get("/{user_id}", response_model=TeamMemberResponse)
async def get_team_member(
    user_id: uuid.UUID, current: CurrentUser = Depends(require_role(*_READ_ROLES))
) -> TeamMemberResponse:
    membership = await _membership_or_404(current, user_id)
    user = await current.session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a member of this company")
    return _to_response(membership, user, await _profile_for(current, user_id), await _profession_map(current))


@router.patch("/{user_id}", response_model=TeamMemberResponse)
async def update_team_member(
    user_id: uuid.UUID,
    payload: MemberProfileUpdateRequest,
    current: CurrentUser = Depends(require_role("admin")),
    _ro: None = Depends(block_if_read_only),
) -> TeamMemberResponse:
    """Update this company's record of a person.

    Creates the profile row on first edit rather than at invitation-accept
    time, so the directory has no half-empty rows for members nobody has
    got round to filling in.
    """
    membership = await _membership_or_404(current, user_id)
    user = await current.session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a member of this company")

    existing = await _profile_for(current, user_id)
    if existing is not None:
        # Immediately after loading and BEFORE the first setattr, so a
        # rejected request stages nothing for the commit get_current_user
        # performs after this handler returns. A profile created below is
        # skipped because there is nothing for it to conflict with.
        guard_stale_write(existing, payload.expected_updated_at, entity_name="team member")
    profile = await _profile_or_create(current, user_id)

    fields = payload.model_fields_set
    for name in (*_CONTACT_FIELDS, "notes"):
        # `model_fields_set` rather than `is not None`: without it there is
        # no way to CLEAR a field, because "absent" and "explicitly null"
        # would both arrive as None.
        if name in fields:
            setattr(profile, name, getattr(payload, name))

    if "profession_id" in fields:
        if payload.profession_id is not None:
            exists = await current.session.scalar(
                select(func.count())
                .select_from(Profession)
                .where(
                    Profession.id == payload.profession_id,
                    Profession.company_id == current.company_id,
                )
            )
            if not exists:
                # 404 rather than 422: from the caller's side an id belonging
                # to another company simply is not a profession they have.
                raise HTTPException(status.HTTP_404_NOT_FOUND, "No such profession")
        profile.profession_id = payload.profession_id

    if payload.phones is not None:
        _replace_phones(current, profile, payload.phones)

    await current.session.flush()

    await write_audit_log(
        current.session,
        company_id=current.company_id,
        actor_id=current.user.id,
        action="team.profile_updated",
        entity_type="member_profile",
        entity_id=profile.id,
        metadata={"user_id": str(user_id), "fields": sorted(fields)},
    )

    return _to_response(membership, user, profile, await _profession_map(current))


@router.post("/{user_id}/photo", response_model=TeamMemberResponse)
async def upload_team_member_photo(
    user_id: uuid.UUID,
    file: UploadFile = File(...),
    current: CurrentUser = Depends(require_role("admin")),
    _ro: None = Depends(block_if_read_only),
) -> TeamMemberResponse:
    membership = await _membership_or_404(current, user_id)
    user = await current.session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a member of this company")

    # Byte-capped read before anything touches disk — see app/core/uploads.py
    # for why a bare `await file.read()` is both a memory and a disk-fill
    # vector. Capped at the branding logo's limit, imported rather than
    # restated: these are the same class of upload, and two literals for one
    # rule is how they drift apart.
    content = await read_upload_limited(file, MAX_LOGO_SIZE_BYTES)
    try:
        relative_path = write_member_photo_file(
            company_id=current.company_id,
            user_id=user_id,
            content_type=file.content_type or "",
            content=content,
        )
    except UnsupportedLogoError as err:
        # 422, matching the branding logo route — a wrong content type is a
        # rejected entity, and answering differently for the same failure on
        # the neighbouring route is the kind of inconsistency a client
        # discovers the hard way.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(err)) from err

    profile = await _profile_or_create(current, user_id)
    profile.image_path = relative_path
    await current.session.flush()

    return _to_response(membership, user, profile, await _profession_map(current))


@router.get("/{user_id}/photo")
async def get_team_member_photo(
    user_id: uuid.UUID, current: CurrentUser = Depends(require_role(*_READ_ROLES))
) -> FileResponse:
    """Serve the photo bytes.

    Behind the same role check as the rest of the directory rather than
    served as a static file: the storage volume holds every tenant's
    uploads, and "unguessable path" is not an access control.
    """
    await _membership_or_404(current, user_id)
    return _photo_response(await _profile_for(current, user_id))


def _photo_response(profile: MemberProfile | None) -> FileResponse:
    """The bytes, or a 404 that says so.

    Shared by `/team/{user_id}/photo` and `/team/me/photo` so the
    missing-file case cannot be handled two ways.
    """
    if profile is None or not profile.image_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No photo for this member")

    absolute = Path(settings.storage_root) / profile.image_path
    if not absolute.exists():
        # The row says there is a photo and the volume disagrees — a restore
        # from a backup that predates the upload will do this. Say so as a
        # 404 rather than a 500.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No photo for this member")
    return FileResponse(absolute)


