"""Task 1.15: `POST /projects/{id}/documents`, `GET /projects/{id}/documents`.

Helper duplication (`_register_and_login`/`_invite_and_login_as`/
`_project_payload`) follows the established per-test-file convention (see
test_leads.py, test_projects.py, test_phases_tasks.py) rather than sharing
them via conftest.py.
"""
from pathlib import Path

import asyncpg

from app.config import settings
from tests.conftest import TEST_DATABASE_URL

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")


async def _register_and_login(client, company_name, email):
    register = await client.post(
        "/auth/register",
        json={
            "company_name": company_name,
            "admin_full_name": "Test Admin",
            "admin_email": email,
            "admin_password": "supersecret123",
        },
    )
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    body = login.json()
    return {
        "company_id": register.json()["company_id"],
        "user_id": register.json()["user_id"],
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
    }


async def _invite_and_login_as(client, admin, role, email):
    invite = await client.post(
        "/invitations",
        json={"email": email, "role": role},
        headers=admin["headers"],
    )
    assert invite.status_code == 201, invite.text
    accept = await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "Invited User", "password": "anothersecret123"},
    )
    assert accept.status_code == 200, accept.text
    login = await client.post("/auth/login", json={"email": email, "password": "anothersecret123"})
    assert login.status_code == 200, login.text

    conn = await asyncpg.connect(OWNER_DSN)
    try:
        user_id = await conn.fetchval("SELECT id FROM users WHERE email = $1", email)
    finally:
        await conn.close()

    return {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}, "user_id": str(user_id)}


def _project_payload(**overrides):
    payload = {
        "name": "Kitchen Remodel",
        "site_address": "123 Main St",
        "projected_start_date": "2026-08-01",
    }
    payload.update(overrides)
    return payload


async def _create_project(client, admin, **overrides):
    response = await client.post("/projects", json=_project_payload(**overrides), headers=admin["headers"])
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _upload(client, actor, project_id, file_name, content: bytes):
    return await client.post(
        f"/projects/{project_id}/documents",
        data={"file_name": file_name},
        files={"file": (file_name, content, "application/octet-stream")},
        headers=actor["headers"],
    )


async def _assign_field_crew_to_project(client, admin, project_id, field_crew_user_id):
    """Field crew's project visibility is scoped via an assigned task
    (_with_field_crew_scope in app/routers/projects.py), not a direct
    project-level grant — uses the real POST /projects/{id}/phases +
    /tasks routes, matching test_phases_tasks.py's own precedent of
    creating fixture data through the API rather than direct-seeding now
    that those routes exist."""
    phase = await client.post(
        f"/projects/{project_id}/phases",
        json={"name": "Foundation", "sequence": 0},
        headers=admin["headers"],
    )
    assert phase.status_code == 201, phase.text
    task = await client.post(
        f"/projects/{project_id}/tasks",
        json={
            "name": "Pour footings",
            "phase_id": phase.json()["id"],
            "assignee_id": field_crew_user_id,
        },
        headers=admin["headers"],
    )
    assert task.status_code == 201, task.text


# --- Upload -------------------------------------------------------------


async def test_admin_can_upload_document(client):
    admin = await _register_and_login(client, "Acme Construction", "doc-admin@acme.test")
    project_id = await _create_project(client, admin)

    response = await _upload(client, admin, project_id, "blueprint.pdf", b"pdf-bytes-v1")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["file_name"] == "blueprint.pdf"
    assert body["version"] == 1
    assert body["project_id"] == project_id
    assert body["company_id"] == admin["company_id"]

    storage_path = body["storage_path"]
    # storage_path must be relative to settings.storage_root, never an
    # absolute filesystem path (design decision #4) — must never leak the
    # server's absolute filesystem layout in an API response.
    assert not storage_path.startswith("/")
    assert not (len(storage_path) > 1 and storage_path[1] == ":")  # no "C:\..." either
    assert storage_path == f"{admin['company_id']}/{project_id}/1/blueprint.pdf"

    on_disk = Path(settings.storage_root) / storage_path
    assert on_disk.is_file()
    assert on_disk.read_bytes() == b"pdf-bytes-v1"


async def test_project_manager_can_upload_document(client):
    admin = await _register_and_login(client, "Acme Construction", "doc-pm-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "doc-pm@acme.test")
    project_id = await _create_project(client, admin)

    response = await _upload(client, pm, project_id, "permit.pdf", b"permit-bytes")
    assert response.status_code == 201, response.text
    assert response.json()["version"] == 1


async def test_field_crew_cannot_upload_document(client):
    admin = await _register_and_login(client, "Acme Construction", "doc-fc-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "doc-fc@acme.test")
    project_id = await _create_project(client, admin)

    response = await _upload(client, field_crew, project_id, "blueprint.pdf", b"x")
    assert response.status_code == 403


async def test_accountant_and_client_cannot_upload_document(client):
    admin = await _register_and_login(client, "Acme Construction", "doc-acct-admin@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "doc-acct@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "doc-client@acme.test")
    project_id = await _create_project(client, admin)

    for actor in (accountant, client_role):
        response = await _upload(client, actor, project_id, "blueprint.pdf", b"x")
        assert response.status_code == 403


# --- Versioning -----------------------------------------------------------


async def test_reupload_same_file_name_creates_new_version_both_retrievable(client):
    admin = await _register_and_login(client, "Acme Construction", "doc-version-admin@acme.test")
    project_id = await _create_project(client, admin)

    first = await _upload(client, admin, project_id, "blueprint.pdf", b"version-one-content")
    assert first.status_code == 201, first.text
    assert first.json()["version"] == 1

    second = await _upload(client, admin, project_id, "blueprint.pdf", b"version-two-content")
    assert second.status_code == 201, second.text
    assert second.json()["version"] == 2

    first_path = first.json()["storage_path"]
    second_path = second.json()["storage_path"]
    assert first_path != second_path

    first_on_disk = Path(settings.storage_root) / first_path
    second_on_disk = Path(settings.storage_root) / second_path

    # Both versions remain on disk, neither overwritten by the other, per
    # US-3.4's "prior versions accessible".
    assert first_on_disk.is_file()
    assert second_on_disk.is_file()
    assert first_on_disk.read_bytes() == b"version-one-content"
    assert second_on_disk.read_bytes() == b"version-two-content"

    # A third upload continues the sequence (not reset, not reusing v1/v2).
    third = await _upload(client, admin, project_id, "blueprint.pdf", b"version-three-content")
    assert third.status_code == 201, third.text
    assert third.json()["version"] == 3


async def test_same_file_name_in_different_projects_is_independent(client):
    """version numbering is scoped per-project, not globally per file_name —
    a same-named file in an unrelated project of the same company starts
    fresh at version 1."""
    admin = await _register_and_login(client, "Acme Construction", "doc-multi-project-admin@acme.test")
    project_a = await _create_project(client, admin, name="Project A")
    project_b = await _create_project(client, admin, name="Project B")

    upload_a = await _upload(client, admin, project_a, "blueprint.pdf", b"project-a-content")
    assert upload_a.status_code == 201, upload_a.text
    assert upload_a.json()["version"] == 1

    upload_b = await _upload(client, admin, project_b, "blueprint.pdf", b"project-b-content")
    assert upload_b.status_code == 201, upload_b.text
    assert upload_b.json()["version"] == 1
    assert upload_b.json()["storage_path"] != upload_a.json()["storage_path"]


# --- Path traversal ---------------------------------------------------------


async def test_path_traversal_file_name_is_rejected_outright(client):
    admin = await _register_and_login(client, "Acme Construction", "doc-traversal-admin@acme.test")
    project_id = await _create_project(client, admin)

    response = await _upload(client, admin, project_id, "../../etc/passwd", b"malicious")
    assert response.status_code == 422, response.text

    # No file was written anywhere as a side effect of the rejected upload —
    # neither inside settings.storage_root under any plausible path, nor
    # anywhere outside it that a naive path-join could have escaped to.
    storage_root = Path(settings.storage_root)
    assert not any(storage_root.rglob("passwd"))

    # No Document row was persisted either.
    list_response = await client.get(f"/projects/{project_id}/documents", headers=admin["headers"])
    assert list_response.status_code == 200, list_response.text
    assert list_response.json()["items"] == []


async def test_backslash_and_absolute_path_file_names_are_rejected(client):
    admin = await _register_and_login(client, "Acme Construction", "doc-traversal2-admin@acme.test")
    project_id = await _create_project(client, admin)

    for bad_name in ("..\\..\\windows\\system32\\config", "/etc/passwd", "C:\\evil.txt", "nested/path.pdf"):
        response = await _upload(client, admin, project_id, bad_name, b"malicious")
        assert response.status_code == 422, f"{bad_name!r} should be rejected, got {response.status_code}"


async def test_control_character_file_name_is_rejected_not_a_500(client):
    # Found during spec review: an embedded NUL byte isn't a traversal
    # sequence, but it's just as attacker-controlled and unfit for a
    # filesystem path — and Postgres' UTF8 text type rejects NUL outright,
    # so an unvalidated one previously reached the DB layer and crashed
    # with an unhandled 500 instead of this module's own clean 422.
    admin = await _register_and_login(client, "Acme Construction", "doc-control-char-admin@acme.test")
    project_id = await _create_project(client, admin)

    response = await _upload(client, admin, project_id, "\x00nullbyte.txt", b"malicious")
    assert response.status_code == 422, response.text

    list_response = await client.get(f"/projects/{project_id}/documents", headers=admin["headers"])
    assert list_response.status_code == 200, list_response.text
    assert list_response.json()["items"] == []


async def test_file_name_over_max_length_is_rejected_not_a_500(client):
    # Found during code-quality review: documents.file_name is String(255);
    # Postgres rejects (doesn't truncate) an overlong VARCHAR insert. An
    # unvalidated over-long file_name previously passed validate_file_name(),
    # wrote to disk successfully, and only failed at the DB insert with an
    # unhandled 500 instead of a clean 422.
    admin = await _register_and_login(client, "Acme Construction", "doc-long-name-admin@acme.test")
    project_id = await _create_project(client, admin)

    over_limit_name = "a" * 256 + ".txt"
    response = await _upload(client, admin, project_id, over_limit_name, b"content")
    assert response.status_code == 422, response.text

    # Boundary check: exactly 255 characters is still accepted.
    at_limit_name = "b" * 251 + ".txt"
    assert len(at_limit_name) == 255
    response = await _upload(client, admin, project_id, at_limit_name, b"content")
    assert response.status_code == 201, response.text


async def test_empty_file_content_uploads_successfully(client):
    admin = await _register_and_login(client, "Acme Construction", "doc-empty-content-admin@acme.test")
    project_id = await _create_project(client, admin)

    response = await _upload(client, admin, project_id, "empty.txt", b"")
    assert response.status_code == 201, response.text

    storage_root = Path(settings.storage_root)
    on_disk = storage_root / response.json()["storage_path"]
    assert on_disk.exists()
    assert on_disk.read_bytes() == b""


async def test_concurrent_upload_of_same_file_name_returns_409_for_the_loser(client):
    # Found during code-quality review: previous_max_version is read via a
    # plain SELECT with no locking, so two genuinely concurrent uploads of
    # the same file_name can both compute the same next `version`. The
    # loser of the on-disk exclusive-create race must get a clean 409, not
    # a silently overwritten file or an unhandled 500 — see
    # write_document_file's docstring in app/services/document_storage.py.
    import asyncio

    admin = await _register_and_login(client, "Acme Construction", "doc-concurrent-admin@acme.test")
    project_id = await _create_project(client, admin)

    responses = await asyncio.gather(
        _upload(client, admin, project_id, "racy.txt", b"first-writer"),
        _upload(client, admin, project_id, "racy.txt", b"second-writer"),
    )
    statuses = sorted(r.status_code for r in responses)
    # Either both happened to serialize cleanly (201, 201 with distinct
    # versions — acceptable, since the race isn't guaranteed to trigger on
    # every run/platform), or one lost the race and got a clean 409, never
    # a 500 and never two 201s claiming the SAME version.
    assert statuses in ([201, 201], [201, 409]), statuses
    if statuses == [201, 201]:
        versions = sorted(r.json()["version"] for r in responses if r.status_code == 201)
        assert versions == [1, 2], versions


# --- Cross-tenant -----------------------------------------------------------


async def test_upload_document_cross_tenant_project_returns_404(client):
    a = await _register_and_login(client, "Company A", "doc-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "doc-cross-b@acme.test")
    project_id = await _create_project(client, b)

    response = await _upload(client, a, project_id, "blueprint.pdf", b"x")
    assert response.status_code == 404


async def test_list_documents_cross_tenant_project_returns_404(client):
    a = await _register_and_login(client, "Company A", "doc-list-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "doc-list-cross-b@acme.test")
    project_id = await _create_project(client, b)
    upload = await _upload(client, b, project_id, "blueprint.pdf", b"x")
    assert upload.status_code == 201, upload.text

    response = await client.get(f"/projects/{project_id}/documents", headers=a["headers"])
    assert response.status_code == 404


async def test_upload_and_list_document_nonexistent_project_returns_404(client):
    # Distinct from the cross-tenant cases above — matches test_projects.py's
    # own convention of testing "doesn't exist at all" separately from
    # "exists but isn't visible to you", even though both route through the
    # same _get_project_or_404 code path.
    admin = await _register_and_login(client, "Acme Construction", "doc-nonexistent-admin@acme.test")
    nonexistent_project_id = "00000000-0000-0000-0000-000000000000"

    upload = await _upload(client, admin, nonexistent_project_id, "blueprint.pdf", b"x")
    assert upload.status_code == 404

    listing = await client.get(f"/projects/{nonexistent_project_id}/documents", headers=admin["headers"])
    assert listing.status_code == 404


# --- List: latest-version-only ----------------------------------------------


async def test_list_documents_shows_latest_version_only(client):
    admin = await _register_and_login(client, "Acme Construction", "doc-list-admin@acme.test")
    project_id = await _create_project(client, admin)

    v1 = await _upload(client, admin, project_id, "blueprint.pdf", b"v1")
    assert v1.status_code == 201, v1.text
    v2 = await _upload(client, admin, project_id, "blueprint.pdf", b"v2")
    assert v2.status_code == 201, v2.text
    other = await _upload(client, admin, project_id, "permit.pdf", b"permit-content")
    assert other.status_code == 201, other.text

    response = await client.get(f"/projects/{project_id}/documents", headers=admin["headers"])
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["items"]) == 2
    by_name = {item["file_name"]: item for item in body["items"]}
    assert set(by_name) == {"blueprint.pdf", "permit.pdf"}
    # blueprint.pdf shows ONLY its latest version (2), never the superseded
    # version 1 alongside it.
    assert by_name["blueprint.pdf"]["version"] == 2
    assert by_name["blueprint.pdf"]["id"] == v2.json()["id"]
    assert by_name["permit.pdf"]["version"] == 1


async def test_list_documents_empty_project_returns_empty_list(client):
    admin = await _register_and_login(client, "Acme Construction", "doc-list-empty-admin@acme.test")
    project_id = await _create_project(client, admin)

    response = await client.get(f"/projects/{project_id}/documents", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_cursor": None}


# --- List: RBAC --------------------------------------------------------------


async def test_admin_pm_accountant_can_list_documents(client):
    admin = await _register_and_login(client, "Acme Construction", "doc-list-rbac-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "doc-list-rbac-pm@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "doc-list-rbac-acct@acme.test")
    project_id = await _create_project(client, admin)
    upload = await _upload(client, admin, project_id, "blueprint.pdf", b"x")
    assert upload.status_code == 201, upload.text

    for actor in (admin, pm, accountant):
        response = await client.get(f"/projects/{project_id}/documents", headers=actor["headers"])
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 1


async def test_field_crew_can_list_documents_for_assigned_project(client):
    admin = await _register_and_login(client, "Acme Construction", "doc-list-fc-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "doc-list-fc@acme.test")
    project_id = await _create_project(client, admin)
    await _assign_field_crew_to_project(client, admin, project_id, field_crew["user_id"])
    upload = await _upload(client, admin, project_id, "blueprint.pdf", b"x")
    assert upload.status_code == 201, upload.text

    response = await client.get(f"/projects/{project_id}/documents", headers=field_crew["headers"])
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1


async def test_field_crew_cannot_list_documents_for_unassigned_project(client):
    admin = await _register_and_login(client, "Acme Construction", "doc-list-fc-none-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "doc-list-fc-none@acme.test")
    project_id = await _create_project(client, admin)
    upload = await _upload(client, admin, project_id, "blueprint.pdf", b"x")
    assert upload.status_code == 201, upload.text

    response = await client.get(f"/projects/{project_id}/documents", headers=field_crew["headers"])
    assert response.status_code == 404


async def test_client_cannot_list_documents(client):
    """Per design decision #8/list_projects's own precedent, `client` gets
    no list-shaped route at all — only the single sanitized GET
    /projects/{id} dashboard. GET /projects/{id}/documents blocks `client`
    with a 403 at the require_role dependency layer, same as list_projects."""
    admin = await _register_and_login(client, "Acme Construction", "doc-list-client-admin@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "doc-list-client@acme.test")
    project_id = await _create_project(client, admin)

    response = await client.get(f"/projects/{project_id}/documents", headers=client_role["headers"])
    assert response.status_code == 403
