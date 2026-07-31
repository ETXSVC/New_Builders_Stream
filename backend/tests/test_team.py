"""The team directory (migration 0026).

The interesting properties are not "a PATCH stores a string". They are:

  1. **The profile is company-scoped, not user-scoped.** The same person in
     two companies has two records, and neither company can read the
     other's. This is the whole reason the data does not live on `users`,
     which has no RLS — so it is tested with one user genuinely belonging
     to two companies, not with two unrelated users.
  2. **Phones replace as a set.** The API has no per-phone id on purpose, so
     the semantics of the list have to be pinned.
  3. **Offboarding takes the record with it.** A profile outliving its
     membership would be an address nobody can see and nobody deletes.
  4. **Reads and writes have different roles**, and a project manager
     picking an assignee must not be able to rewrite an HR record.
  5. **Photo bytes are served by a route, not by the file server.** One
     volume holds every tenant's uploads, so the path is not the boundary.
"""
import uuid
from pathlib import Path

import asyncpg

from app.config import settings
from tests.conftest import TEST_DATABASE_URL, register_and_login

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")
PASSWORD = "supersecret123"


async def _add_member(client, host: dict, email: str, role: str) -> dict:
    """Invite somebody and accept, through the real endpoints.

    Not a direct INSERT: the accept flow is what creates the membership this
    module hangs off, so exercising it here keeps the two in step.
    """
    invite = await client.post(
        "/invitations", json={"email": email, "role": role}, headers=host["headers"]
    )
    assert invite.status_code == 201, invite.text

    accepted = await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "Invited Person", "password": PASSWORD},
    )
    assert accepted.status_code == 200, accepted.text

    login = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    body = login.json()

    # TokenResponse deliberately carries no user_id, so resolve it from the
    # membership roster the host can already see — which also confirms the
    # accept actually produced a membership rather than just a user.
    roster = await client.get("/companies/members", headers=host["headers"])
    assert roster.status_code == 200, roster.text
    match = next(m for m in roster.json()["items"] if m["email"] == email)

    return {
        "user_id": match["user_id"],
        "headers": {
            "Authorization": f"Bearer {body['access_token']}",
            "X-Tenant-ID": body["default_company_id"],
        },
    }


async def _member_in_list(client, headers, user_id: str) -> dict | None:
    response = await client.get("/team", headers=headers)
    assert response.status_code == 200, response.text
    return next((m for m in response.json()["items"] if m["user_id"] == user_id), None)


# --------------------------------------------------------------------------
# 1. The directory lists people, profile or not
# --------------------------------------------------------------------------


async def test_a_member_appears_before_anyone_fills_in_their_details(client):
    """A profile row is created on first EDIT, not at accept time. Somebody
    who just joined must still show up — with their account details and
    empty everything else — or the directory would hide new joiners until an
    admin happened to open them."""
    host = await register_and_login(client, "Acme Build", "admin@acme-team.example")
    member = await _add_member(client, host, "new@acme-team.example", "field_crew")

    listed = await _member_in_list(client, host["headers"], member["user_id"])

    assert listed is not None
    assert listed["email"] == "new@acme-team.example"
    assert listed["role"] == "field_crew"
    assert listed["first_name"] is None
    assert listed["phones"] == []
    assert listed["has_photo"] is False
    # No profile row yet, so nothing to conflict with on a first write.
    assert listed["updated_at"] is None


async def test_every_requested_field_round_trips(client):
    host = await register_and_login(client, "Fields Co", "admin@fields.example")
    member = await _add_member(client, host, "person@fields.example", "project_manager")

    trade = await client.post(
        "/team/professions", json={"name": "Electrician"}, headers=host["headers"]
    )
    assert trade.status_code == 201, trade.text

    response = await client.patch(
        f"/team/{member['user_id']}",
        json={
            "first_name": "Dale",
            "last_name": "Rivera",
            "address_line1": "144 Mill Road",
            "address_line2": "Unit 3",
            "city": "Tyler",
            "state": "TX",
            "postal_code": "75701",
            "notes": "Prefers early starts. Has own tools.",
            "profession_id": trade.json()["id"],
            "phones": [
                {"label": "mobile", "number": "+1 903 555 0142"},
                {"label": "site trailer", "number": "903-555-0199 x12"},
            ],
        },
        headers=host["headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["first_name"] == "Dale"
    assert body["last_name"] == "Rivera"
    assert body["address_line1"] == "144 Mill Road"
    assert body["address_line2"] == "Unit 3"
    assert body["city"] == "Tyler"
    assert body["state"] == "TX"
    assert body["postal_code"] == "75701"
    assert body["notes"].startswith("Prefers early starts")
    assert body["profession"]["name"] == "Electrician"
    # Stored as typed — the extension survives, which a normalising parser
    # would have thrown away.
    assert [p["number"] for p in body["phones"]] == [
        "+1 903 555 0142",
        "903-555-0199 x12",
    ]
    # Email is the login and is not editable here.
    assert body["email"] == "person@fields.example"


# --------------------------------------------------------------------------
# 2. Phones are a set
# --------------------------------------------------------------------------


async def test_phones_replace_rather_than_append(client):
    """The list sent IS the list. Pinned because the API deliberately has no
    per-phone id, so "add" and "replace" would otherwise be ambiguous."""
    host = await register_and_login(client, "Phones Co", "admin@phones.example")
    member = await _add_member(client, host, "p@phones.example", "field_crew")
    url = f"/team/{member['user_id']}"

    await client.patch(
        url,
        json={"phones": [{"label": "mobile", "number": "111"}, {"label": "home", "number": "222"}]},
        headers=host["headers"],
    )

    replaced = await client.patch(
        url, json={"phones": [{"label": "mobile", "number": "333"}]}, headers=host["headers"]
    )
    assert [p["number"] for p in replaced.json()["phones"]] == ["333"]

    cleared = await client.patch(url, json={"phones": []}, headers=host["headers"])
    assert cleared.json()["phones"] == []


async def test_omitting_phones_leaves_them_alone(client):
    """`None` means "leave alone" and `[]` means "remove all" — the
    distinction the whole PATCH depends on."""
    host = await register_and_login(client, "Keep Co", "admin@keep.example")
    member = await _add_member(client, host, "k@keep.example", "field_crew")
    url = f"/team/{member['user_id']}"

    await client.patch(
        url, json={"phones": [{"label": "mobile", "number": "555"}]}, headers=host["headers"]
    )
    untouched = await client.patch(url, json={"city": "Dallas"}, headers=host["headers"])

    assert untouched.json()["city"] == "Dallas"
    assert [p["number"] for p in untouched.json()["phones"]] == ["555"]


async def test_a_field_can_be_cleared(client):
    """`model_fields_set`, not `is not None` — without it there would be no
    way to erase a note somebody typed by mistake."""
    host = await register_and_login(client, "Clear Co", "admin@clear.example")
    member = await _add_member(client, host, "c@clear.example", "field_crew")
    url = f"/team/{member['user_id']}"

    await client.patch(url, json={"notes": "temporary"}, headers=host["headers"])
    cleared = await client.patch(url, json={"notes": None}, headers=host["headers"])

    assert cleared.json()["notes"] is None


# --------------------------------------------------------------------------
# 3. Tenancy — the reason this table exists at all
# --------------------------------------------------------------------------


async def test_two_companies_keep_separate_records_of_the_same_person(client):
    """The property the whole design rests on.

    One human, two employers, two directories. If this data lived on `users`
    (which has NO row-level security) both companies would read the same
    row — including a manager's private notes. A two-user version of this
    test would pass against that broken design, so the shared person is the
    point.
    """
    first = await register_and_login(client, "First Employer", "admin@first-emp.example")
    second = await register_and_login(client, "Second Employer", "admin@second-emp.example")

    shared_email = "moonlighter@example.com"
    in_first = await _add_member(client, first, shared_email, "accountant")

    # The second membership is written directly, and that is a statement
    # about the product rather than a shortcut. `company_users` is a
    # many-to-many by design and `get_current_user` verifies a claimed tenant
    # against it — but no ROUTE puts an EXISTING account into a second
    # company today: `POST /invitations/{id}/accept` always creates a user
    # and answers 409 "Email already registered" when one exists. Waiting for
    # that flow would mean leaving the property this table's whole shape was
    # chosen for untested. Owner connection because inserting a membership
    # into a company nobody is authenticated as is precisely what RLS stops.
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO company_users (company_id, user_id, role) VALUES ($1, $2, 'accountant')",
            uuid.UUID(second["company_id"]),
            uuid.UUID(in_first["user_id"]),
        )
    finally:
        await conn.close()

    noted = await client.patch(
        f"/team/{in_first['user_id']}",
        json={"notes": "Only works Tuesdays for us."},
        headers=first["headers"],
    )
    assert noted.status_code == 200, noted.text

    # The second employer sees the same person with a blank record.
    seen_by_second = await _member_in_list(client, second["headers"], in_first["user_id"])
    assert seen_by_second is not None, "the shared user should be a member of both companies"
    assert seen_by_second["notes"] is None

    # ...and the first employer's own note is untouched by the read above —
    # two records, not one shared row seen twice.
    seen_by_first = await _member_in_list(client, first["headers"], in_first["user_id"])
    assert seen_by_first is not None
    assert seen_by_first["notes"] == "Only works Tuesdays for us."


async def test_another_company_cannot_read_or_write_a_member(client):
    host = await register_and_login(client, "Inside Co", "admin@inside.example")
    outsider = await register_and_login(client, "Outside Co", "admin@outside.example")
    member = await _add_member(client, host, "m@inside.example", "field_crew")

    read = await client.get(f"/team/{member['user_id']}", headers=outsider["headers"])
    assert read.status_code == 404, read.text

    write = await client.patch(
        f"/team/{member['user_id']}", json={"city": "Nope"}, headers=outsider["headers"]
    )
    assert write.status_code == 404, write.text


# --------------------------------------------------------------------------
# 4. Roles
# --------------------------------------------------------------------------


async def test_a_project_manager_reads_but_cannot_write(client):
    """Reads match GET /companies/members (an assignee picker needs them);
    writes are admin-only because a directory entry is an HR record."""
    host = await register_and_login(client, "Roles Co", "admin@roles.example")
    pm = await _add_member(client, host, "pm@roles.example", "project_manager")

    assert (await client.get("/team", headers=pm["headers"])).status_code == 200

    denied = await client.patch(
        f"/team/{pm['user_id']}", json={"city": "Austin"}, headers=pm["headers"]
    )
    assert denied.status_code == 403, denied.text


async def test_field_crew_cannot_read_the_directory(client):
    host = await register_and_login(client, "Crew Co", "admin@crew.example")
    crew = await _add_member(client, host, "crew@crew.example", "field_crew")

    response = await client.get("/team", headers=crew["headers"])

    assert response.status_code == 403, response.text


# --------------------------------------------------------------------------
# 5. Professions
# --------------------------------------------------------------------------


async def test_professions_are_unique_per_company_case_insensitively(client):
    """A dropdown offering both "Electrician" and "electrician" is a
    data-entry bug. Enforced by a functional index, so it holds against a
    race rather than a pre-check."""
    host = await register_and_login(client, "Trades Co", "admin@trades.example")

    first = await client.post(
        "/team/professions", json={"name": "Framer"}, headers=host["headers"]
    )
    assert first.status_code == 201, first.text

    dupe = await client.post(
        "/team/professions", json={"name": "framer"}, headers=host["headers"]
    )
    assert dupe.status_code == 409, dupe.text


async def test_the_profession_list_is_company_scoped_and_reachable(client):
    """Two properties in one request. The list is the caller's own trades —
    and `GET /team/professions` resolves to THIS route rather than being
    swallowed by `GET /team/{user_id}`, which is true only because the
    professions routes are declared first. A 422 here would mean somebody
    reordered the router."""
    host = await register_and_login(client, "List Trades Co", "admin@listtrades.example")
    other = await register_and_login(client, "Their Trades Co", "admin@theirtrades.example")

    for name in ("Welder", "Carpenter"):
        created = await client.post(
            "/team/professions", json={"name": name}, headers=host["headers"]
        )
        assert created.status_code == 201, created.text
    await client.post("/team/professions", json={"name": "Glazier"}, headers=other["headers"])

    response = await client.get("/team/professions", headers=host["headers"])

    assert response.status_code == 200, response.text
    assert [p["name"] for p in response.json()] == ["Carpenter", "Welder"]


async def test_another_companys_profession_cannot_be_assigned(client):
    host = await register_and_login(client, "Mine Co", "admin@mine.example")
    other = await register_and_login(client, "Theirs Co", "admin@theirs.example")
    member = await _add_member(client, host, "m@mine.example", "field_crew")

    theirs = await client.post(
        "/team/professions", json={"name": "Roofer"}, headers=other["headers"]
    )
    assert theirs.status_code == 201, theirs.text

    response = await client.patch(
        f"/team/{member['user_id']}",
        json={"profession_id": theirs.json()["id"]},
        headers=host["headers"],
    )
    # 404, not 422: from this company's side that id simply is not one of
    # their professions.
    assert response.status_code == 404, response.text


async def test_deleting_a_profession_releases_its_holders(client):
    """SET NULL, not RESTRICT and not CASCADE: retiring a trade must neither
    be blocked by whoever holds it nor delete those people."""
    host = await register_and_login(client, "Retire Co", "admin@retire.example")
    member = await _add_member(client, host, "m@retire.example", "field_crew")

    trade = await client.post(
        "/team/professions", json={"name": "Painter"}, headers=host["headers"]
    )
    trade_id = trade.json()["id"]
    await client.patch(
        f"/team/{member['user_id']}", json={"profession_id": trade_id}, headers=host["headers"]
    )

    deleted = await client.delete(f"/team/professions/{trade_id}", headers=host["headers"])
    assert deleted.status_code == 204, deleted.text

    still_there = await client.get(f"/team/{member['user_id']}", headers=host["headers"])
    assert still_there.status_code == 200
    assert still_there.json()["profession"] is None


# --------------------------------------------------------------------------
# 6. Photos
# --------------------------------------------------------------------------


async def test_a_photo_round_trips_and_only_the_company_can_read_it(client):
    """The bytes are served by a ROUTE behind the directory's role check, not
    as a static file. The volume holds every tenant's uploads, so an
    unguessable path would be the only thing between one company's storage
    and another's — which is not an access control."""
    host = await register_and_login(client, "Photo Co", "admin@photo.example")
    outsider = await register_and_login(client, "Other Co", "admin@photo-other.example")
    member = await _add_member(client, host, "face@photo.example", "field_crew")
    content = b"\x89PNG\r\n\x1a\n" + b"x" * 512

    uploaded = await client.post(
        f"/team/{member['user_id']}/photo",
        files={"file": ("face.png", content, "image/png")},
        headers=host["headers"],
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["has_photo"] is True

    # Under the tenant's own subtree, like every other upload on the volume.
    on_disk = (
        Path(settings.storage_root) / host["company_id"] / "team" / f"{member['user_id']}.png"
    )
    assert on_disk.exists()

    served = await client.get(f"/team/{member['user_id']}/photo", headers=host["headers"])
    assert served.status_code == 200, served.text
    assert served.content == content

    # 404 rather than 403, for the same reason the by-id lookups answer 404:
    # a member of another company is not a member this caller may probe for.
    denied = await client.get(
        f"/team/{member['user_id']}/photo", headers=outsider["headers"]
    )
    assert denied.status_code == 404, denied.text


async def test_a_member_without_a_photo_is_a_404_not_a_500(client):
    host = await register_and_login(client, "No Photo Co", "admin@nophoto.example")
    member = await _add_member(client, host, "m@nophoto.example", "field_crew")

    response = await client.get(f"/team/{member['user_id']}/photo", headers=host["headers"])

    assert response.status_code == 404, response.text


async def test_a_photo_that_is_not_an_image_is_refused(client):
    """The allowlist is the branding logo's, imported rather than restated —
    notably SVG is NOT on it, because an SVG is a script delivery vehicle."""
    host = await register_and_login(client, "Bad Photo Co", "admin@badphoto.example")
    member = await _add_member(client, host, "m@badphoto.example", "field_crew")

    refused = await client.post(
        f"/team/{member['user_id']}/photo",
        files={"file": ("payload.svg", b"<svg onload='alert(1)'/>", "image/svg+xml")},
        headers=host["headers"],
    )
    assert refused.status_code == 422, refused.text

    listed = await _member_in_list(client, host["headers"], member["user_id"])
    assert listed is not None
    assert listed["has_photo"] is False


async def test_a_project_manager_cannot_change_a_photo(client):
    """Reads are shared with project_manager; writes are not — a photo is
    part of the same HR record as the address next to it."""
    host = await register_and_login(client, "Photo Roles Co", "admin@photoroles.example")
    pm = await _add_member(client, host, "pm@photoroles.example", "project_manager")

    denied = await client.post(
        f"/team/{pm['user_id']}/photo",
        files={"file": ("face.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        headers=pm["headers"],
    )

    assert denied.status_code == 403, denied.text


# --------------------------------------------------------------------------
# 7. Your own record
# --------------------------------------------------------------------------


async def test_a_member_edits_their_own_contact_details(client):
    """`/team/me` exists so somebody who cannot open the directory at all can
    still fix their own phone number. Field crew is the case that proves it:
    they are 403 on every other route in this module."""
    host = await register_and_login(client, "Self Co", "admin@self.example")
    crew = await _add_member(client, host, "crew@self.example", "field_crew")

    assert (await client.get("/team", headers=crew["headers"])).status_code == 403

    saved = await client.patch(
        "/team/me",
        json={
            "first_name": "Sam",
            "last_name": "Okafor",
            "city": "Longview",
            "phones": [{"label": "mobile", "number": "903-555-0102"}],
        },
        headers=crew["headers"],
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["first_name"] == "Sam"
    assert [p["number"] for p in saved.json()["phones"]] == ["903-555-0102"]

    # And the admin sees it in the directory — one record, two doors.
    listed = await _member_in_list(client, host["headers"], crew["user_id"])
    assert listed is not None
    assert listed["first_name"] == "Sam"
    assert listed["city"] == "Longview"


async def test_a_member_cannot_write_the_fields_that_are_not_theirs(client):
    """`notes` is the company's record ABOUT somebody and `profession_id` is
    how the company classifies them. `MemberSelfUpdateRequest` does not have
    either field and forbids extras, so this is a 422 rather than a silently
    dropped key that reports success."""
    host = await register_and_login(client, "Bounds Co", "admin@bounds.example")
    crew = await _add_member(client, host, "crew@bounds.example", "field_crew")
    trade = await client.post(
        "/team/professions", json={"name": "Framer"}, headers=host["headers"]
    )

    refused_notes = await client.patch(
        "/team/me", json={"notes": "I am a delight to work with"}, headers=crew["headers"]
    )
    assert refused_notes.status_code == 422, refused_notes.text

    refused_trade = await client.patch(
        "/team/me",
        json={"profession_id": trade.json()["id"]},
        headers=crew["headers"],
    )
    assert refused_trade.status_code == 422, refused_trade.text


async def test_a_member_never_reads_the_notes_kept_about_them(client):
    """The read half of the same rule. Withholding the write while handing
    the value back would be theatre."""
    host = await register_and_login(client, "Notes Co", "admin@notes.example")
    crew = await _add_member(client, host, "crew@notes.example", "field_crew")

    await client.patch(
        f"/team/{crew['user_id']}",
        json={"notes": "Do not put on the Harrison job", "city": "Kilgore"},
        headers=host["headers"],
    )

    mine = await client.get("/team/me", headers=crew["headers"])
    assert mine.status_code == 200, mine.text
    # The rest of the record is theirs to see; the note is not.
    assert mine.json()["city"] == "Kilgore"
    assert mine.json()["notes"] is None

    # And it is still there for the people it was written for.
    assert (
        (await client.get(f"/team/{crew['user_id']}", headers=host["headers"])).json()["notes"]
        == "Do not put on the Harrison job"
    )


async def test_a_member_still_cannot_read_somebody_else(client):
    """Self-service is not a way into the directory."""
    host = await register_and_login(client, "Nosy Co", "admin@nosy.example")
    crew = await _add_member(client, host, "crew@nosy.example", "field_crew")
    other = await _add_member(client, host, "other@nosy.example", "field_crew")

    assert (
        await client.get(f"/team/{other['user_id']}", headers=crew["headers"])
    ).status_code == 403
    assert (
        await client.patch(
            f"/team/{other['user_id']}", json={"city": "Nope"}, headers=crew["headers"]
        )
    ).status_code == 403


async def test_a_client_has_no_profile_of_their_own(client):
    """A client is somebody else's customer with row-scoped access to a
    project, not one of this company's people — so they are not in
    `_SELF_ROLES` and `/team/me` is not theirs."""
    host = await register_and_login(client, "Client Co", "admin@clientco.example")
    outsider = await _add_member(client, host, "buyer@clientco.example", "client")

    assert (await client.get("/team/me", headers=outsider["headers"])).status_code == 403
    assert (
        await client.patch("/team/me", json={"city": "Nope"}, headers=outsider["headers"])
    ).status_code == 403


async def test_your_own_photo_round_trips_without_the_directory(client):
    host = await register_and_login(client, "Selfie Co", "admin@selfie.example")
    crew = await _add_member(client, host, "crew@selfie.example", "field_crew")
    content = b"\x89PNG\r\n\x1a\n" + b"y" * 256

    uploaded = await client.post(
        "/team/me/photo",
        files={"file": ("me.png", content, "image/png")},
        headers=crew["headers"],
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["has_photo"] is True

    served = await client.get("/team/me/photo", headers=crew["headers"])
    assert served.status_code == 200
    assert served.content == content

    # The same bytes, reached the admin's way.
    assert (
        await client.get(f"/team/{crew['user_id']}/photo", headers=host["headers"])
    ).status_code == 200


async def test_your_own_save_is_guarded_against_a_concurrent_admin_edit(client):
    """The admin and the member can be in the same record at once, which is
    the version of this race most likely to happen."""
    host = await register_and_login(client, "Race Me Co", "admin@raceme.example")
    crew = await _add_member(client, host, "crew@raceme.example", "field_crew")

    mine = await client.patch("/team/me", json={"city": "Tyler"}, headers=crew["headers"])
    stale_token = mine.json()["updated_at"]

    await client.patch(
        f"/team/{crew['user_id']}", json={"city": "Athens"}, headers=host["headers"]
    )

    conflicted = await client.patch(
        "/team/me",
        json={"city": "Palestine", "expected_updated_at": stale_token},
        headers=crew["headers"],
    )
    assert conflicted.status_code == 409, conflicted.text


# --------------------------------------------------------------------------
# 8. What the assignee picker sees
# --------------------------------------------------------------------------


async def test_the_member_list_carries_the_filed_name_and_trade(client):
    """`GET /companies/members` is the assignee picker's data source and could
    only ever offer `users.full_name` — the name somebody set on their own
    ACCOUNT. The directory holds what this company calls them and what they
    do, which is what "who should do this?" actually needs."""
    host = await register_and_login(client, "Picker Co", "admin@picker.example")
    member = await _add_member(client, host, "sparks@picker.example", "field_crew")
    trade = await client.post(
        "/team/professions", json={"name": "Electrician"}, headers=host["headers"]
    )
    await client.patch(
        f"/team/{member['user_id']}",
        json={
            "first_name": "Rosa",
            "last_name": "Okafor",
            "profession_id": trade.json()["id"],
            "notes": "Only works Tuesdays",
            "city": "Tyler",
        },
        headers=host["headers"],
    )

    listed = await client.get("/companies/members", headers=host["headers"])
    assert listed.status_code == 200, listed.text
    row = next(m for m in listed.json()["items"] if m["user_id"] == member["user_id"])

    assert row["filed_name"] == "Rosa Okafor"
    assert row["profession"] == "Electrician"
    # The account name is still there to fall back on, and is NOT overwritten
    # — the two names are separate facts about the same person.
    assert row["full_name"] == "Invited Person"

    # A dropdown needs a name and a trade. It does not need an address, and
    # it must not carry the notes kept about somebody — the directory
    # withholds those even from their subject, and a picker payload is a far
    # wider audience than that.
    assert "notes" not in row
    assert "city" not in row


async def test_a_member_with_no_profile_still_appears_in_the_picker(client):
    """The fallback the picker relies on: a profile row exists only once
    somebody has filled one in, so most people have none on day one."""
    host = await register_and_login(client, "Fresh Co", "admin@fresh.example")
    member = await _add_member(client, host, "new@fresh.example", "field_crew")

    listed = await client.get("/companies/members", headers=host["headers"])
    row = next(m for m in listed.json()["items"] if m["user_id"] == member["user_id"])

    assert row["filed_name"] is None
    assert row["profession"] is None
    assert row["full_name"] == "Invited Person"


async def test_the_picker_shows_this_companys_record_not_another_companys(client):
    """The property the whole table shape exists for, seen from the picker:
    one person, two employers, and the name each company filed them under."""
    first = await register_and_login(client, "Filed First", "admin@filed-first.example")
    second = await register_and_login(client, "Filed Second", "admin@filed-second.example")
    shared_email = "shared@filed.example"
    person = await _add_member(client, first, shared_email, "field_crew")

    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO company_users (company_id, user_id, role) VALUES ($1, $2, 'field_crew')",
            uuid.UUID(second["company_id"]),
            uuid.UUID(person["user_id"]),
        )
    finally:
        await conn.close()

    await client.patch(
        f"/team/{person['user_id']}",
        json={"first_name": "Bo", "last_name": "Iyer"},
        headers=first["headers"],
    )

    seen_by_second = await client.get("/companies/members", headers=second["headers"])
    row = next(
        m for m in seen_by_second.json()["items"] if m["user_id"] == person["user_id"]
    )
    assert row["filed_name"] is None, "the other employer's record must not leak into this picker"


# --------------------------------------------------------------------------
# 9. Offboarding, and concurrent edits
# --------------------------------------------------------------------------


async def test_removing_a_member_takes_their_record_with_them(client):
    """The composite FK's ON DELETE CASCADE. Without it, offboarding leaves
    an address and a phone number in a table no screen ever shows again and
    no cleanup job knows about."""
    host = await register_and_login(client, "Exit Co", "admin@exit.example")
    member = await _add_member(client, host, "leaver@exit.example", "field_crew")
    await client.patch(
        f"/team/{member['user_id']}",
        json={"city": "Waco", "phones": [{"label": "mobile", "number": "999"}]},
        headers=host["headers"],
    )

    removed = await client.delete(
        f"/companies/members/{member['user_id']}", headers=host["headers"]
    )
    assert removed.status_code in (200, 204), removed.text

    conn = await asyncpg.connect(OWNER_DSN)
    try:
        profiles = await conn.fetchval(
            "SELECT count(*) FROM member_profiles WHERE user_id = $1",
            uuid.UUID(member["user_id"]),
        )
        # Owner connection, so RLS is not what is hiding these — they are
        # genuinely gone, phones included via their own cascade.
        phones = await conn.fetchval(
            "SELECT count(*) FROM member_phones mp "
            "JOIN member_profiles p ON p.id = mp.member_profile_id WHERE p.user_id = $1",
            uuid.UUID(member["user_id"]),
        )
    finally:
        await conn.close()

    assert profiles == 0
    assert phones == 0


async def test_a_concurrent_edit_is_refused_rather_than_silently_overwritten(client):
    """Two admins with the same person open. Without the guard the second
    save discards the first's phone numbers with no 409 and no trace."""
    host = await register_and_login(client, "Race Co", "admin@race.example")
    member = await _add_member(client, host, "m@race.example", "field_crew")
    url = f"/team/{member['user_id']}"

    first = await client.patch(url, json={"city": "Plano"}, headers=host["headers"])
    stale_token = first.json()["updated_at"]

    # Somebody else saves in between.
    await client.patch(url, json={"city": "Frisco"}, headers=host["headers"])

    conflicted = await client.patch(
        url,
        json={"city": "Denton", "expected_updated_at": stale_token},
        headers=host["headers"],
    )
    assert conflicted.status_code == 409, conflicted.text

    # And the guard is opt-in: the same write without the token succeeds.
    assert (await client.patch(url, json={"city": "Denton"}, headers=host["headers"])).status_code == 200
