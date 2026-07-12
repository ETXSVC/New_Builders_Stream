"""Task 2.18: `capture_esignature` (`app/services/esignature.py`) and
`GET /esignatures/{id}` (`app/routers/esignatures.py`).

`capture_esignature` is tested by calling it DIRECTLY as a service
function, not through any HTTP route — this task adds no capture-triggering
route at all (Tasks 2.19/2.22 do that later, calling this same shared
function from their own approval endpoints). Follows
`tests/test_cost_catalog_inheritance.py`'s `_scoped_session` pattern
(`set_current_user` then `set_current_tenant` on a raw `SessionLocal()`
session, mirroring `get_current_user`'s own sequence) rather than
`test_estimate_calculation.py`'s pure-HTTP style, since there is no HTTP
route to drive this function through.

Helper duplication (`_register_and_login`/`_invite_and_login_as`) follows
the established per-test-file convention (see test_leads.py,
test_estimate_calculation.py) rather than sharing them via conftest.py.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from app.config import settings
from app.db import SessionLocal, set_current_tenant, set_current_user
from app.schemas.esignature import EsignatureCaptureRequest
from app.services.esignature import capture_esignature
from tests.conftest import TEST_APP_DATABASE_URL, TEST_DATABASE_URL

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")
APP_CONN_DSN = TEST_APP_DATABASE_URL.replace("+asyncpg", "")


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
    return {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}


@asynccontextmanager
async def _scoped_session(company_id, user_id):
    """Mirrors get_current_user's own context-setting sequence
    (set_current_user then set_current_tenant, inside one open transaction)
    without a real HTTP request — see `test_cost_catalog_inheritance.py`'s
    identically named helper for the full rationale, including why this
    commits (rather than rolls back) on the way out."""
    session = SessionLocal()
    try:
        await session.begin()
        await set_current_user(session, str(user_id))
        await set_current_tenant(session, str(company_id))
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def _capture(company_id, user_id, **overrides):
    payload = {
        "company_id": company_id,
        "signer_name": "Jane Client",
        "signer_email": "jane@client.test",
        "ip_address": "203.0.113.42",
        "document_type": "estimate",
        "signature_artifact_bytes": b"fake-png-bytes",
    }
    payload.update(overrides)
    async with _scoped_session(company_id, user_id) as session:
        esignature = await capture_esignature(session, **payload)
        # Read back everything needed post-commit while the object is still
        # attached; expire_on_commit=False (SessionLocal's own config) keeps
        # these readable afterward regardless, but this keeps the call sites
        # below simple.
        return esignature


# =============================================================================
# EsignatureCaptureRequest never accepts signed_at/ip_address from a client
# =============================================================================


def test_capture_request_schema_excludes_signed_at_and_ip_address():
    """Direct proof that signed_at/ip_address are structurally impossible to
    supply via `EsignatureCaptureRequest` — they're not merely ignored, the
    fields don't exist on the schema at all."""
    assert "signed_at" not in EsignatureCaptureRequest.model_fields
    assert "ip_address" not in EsignatureCaptureRequest.model_fields
    assert set(EsignatureCaptureRequest.model_fields) == {"signer_name", "signer_email"}


# =============================================================================
# capture_esignature: correct, immutable row + real (not client-supplied)
# signed_at/ip_address + artifact file written to disk
# =============================================================================


async def test_capture_esignature_produces_correct_row_with_real_signed_at(client):
    admin = await _register_and_login(client, "Acme Construction", "esig-capture-admin@acme.test")
    company_id = uuid.UUID(admin["company_id"])
    user_id = uuid.UUID(admin["user_id"])

    before = datetime.now(timezone.utc)
    esignature = await _capture(company_id, user_id)
    after = datetime.now(timezone.utc)

    # capture_esignature's own signature has no signed_at parameter at all
    # (test above proves the same for the request schema) — the only way
    # signed_at could be anything other than the real capture-time moment is
    # a bug in the function body, which this window check would catch.
    assert before <= esignature.signed_at <= after
    assert esignature.ip_address == "203.0.113.42"
    assert esignature.signer_name == "Jane Client"
    assert esignature.signer_email == "jane@client.test"
    assert esignature.document_type == "estimate"
    assert esignature.company_id == company_id
    assert esignature.signature_artifact_path == f"{company_id}/esignatures/{esignature.id}.png"

    # Persisted for real (not just an in-memory ORM object) — read back via
    # the owner connection, bypassing RLS/the ORM session entirely.
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        row = await conn.fetchrow("SELECT * FROM esignatures WHERE id = $1", esignature.id)
    finally:
        await conn.close()
    assert row is not None
    assert row["signer_name"] == "Jane Client"
    assert row["signer_email"] == "jane@client.test"
    assert str(row["ip_address"]) == "203.0.113.42"
    assert str(row["company_id"]) == str(company_id)
    assert row["document_type"] == "estimate"


async def test_capture_esignature_writes_artifact_file_to_disk(client):
    admin = await _register_and_login(client, "Acme Construction", "esig-artifact-admin@acme.test")
    company_id = uuid.UUID(admin["company_id"])
    user_id = uuid.UUID(admin["user_id"])

    esignature = await _capture(
        company_id, user_id, signature_artifact_bytes=b"\x89PNG\r\n\x1a\nfake-signature-content"
    )

    artifact_path = Path(settings.storage_root) / str(company_id) / "esignatures" / f"{esignature.id}.png"
    assert artifact_path.exists()
    assert artifact_path.read_bytes() == b"\x89PNG\r\n\x1a\nfake-signature-content"


async def test_capture_esignature_rejects_invalid_document_type_without_orphaning_a_file(client):
    """Regression test for a real bug caught during this task's review:
    `capture_esignature` used to write the artifact file BEFORE validating
    `document_type`, so a rejected insert (the DB's own
    `ck_esignatures_document_type` CHECK firing) still left a real,
    orphaned signature-artifact file on disk with no corresponding row and
    no way to ever discover it — a filesystem write has no transactional
    relationship to the DB write that follows it. Fixed by validating
    `document_type` against `VALID_DOCUMENT_TYPES` in Python first, so a
    bad value never reaches the file-write step at all. This asserts BOTH
    halves: the call raises, AND no file was written for the id that would
    have been used."""
    admin = await _register_and_login(client, "Acme Construction", "esig-badtype-admin@acme.test")
    company_id = uuid.UUID(admin["company_id"])
    user_id = uuid.UUID(admin["user_id"])

    esignatures_dir = Path(settings.storage_root) / str(company_id) / "esignatures"
    files_before = set(esignatures_dir.glob("*.png")) if esignatures_dir.exists() else set()

    with pytest.raises(ValueError, match="document_type"):
        await _capture(company_id, user_id, document_type="not_a_real_type")

    files_after = set(esignatures_dir.glob("*.png")) if esignatures_dir.exists() else set()
    assert files_after == files_before, "no artifact file should be written for a rejected document_type"


# =============================================================================
# Immutability: raw UPDATE/DELETE against esignatures as app_user rejected
# (Task 2.17's REVOKE, same discipline as every prior immutability test)
# =============================================================================


async def test_raw_update_against_esignatures_as_app_user_is_rejected(client):
    admin = await _register_and_login(client, "Acme Construction", "esig-update-admin@acme.test")
    company_id = uuid.UUID(admin["company_id"])
    user_id = uuid.UUID(admin["user_id"])

    esignature = await _capture(company_id, user_id)

    app_conn = await asyncpg.connect(APP_CONN_DSN)
    try:
        await app_conn.execute(
            "SELECT set_config('app.current_tenant', $1, false)", str(company_id)
        )
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await app_conn.execute(
                "UPDATE esignatures SET signer_name = 'Hacked' WHERE id = $1", esignature.id
            )
    finally:
        await app_conn.close()


async def test_raw_delete_against_esignatures_as_app_user_is_rejected(client):
    admin = await _register_and_login(client, "Acme Construction", "esig-delete-admin@acme.test")
    company_id = uuid.UUID(admin["company_id"])
    user_id = uuid.UUID(admin["user_id"])

    esignature = await _capture(company_id, user_id)

    app_conn = await asyncpg.connect(APP_CONN_DSN)
    try:
        await app_conn.execute(
            "SELECT set_config('app.current_tenant', $1, false)", str(company_id)
        )
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await app_conn.execute("DELETE FROM esignatures WHERE id = $1", esignature.id)
    finally:
        await app_conn.close()


# =============================================================================
# GET /esignatures/{id}
# =============================================================================


async def test_get_esignature_returns_captured_record(client):
    admin = await _register_and_login(client, "Acme Construction", "esig-get-admin@acme.test")
    company_id = uuid.UUID(admin["company_id"])
    user_id = uuid.UUID(admin["user_id"])

    esignature = await _capture(company_id, user_id, document_type="change_order")

    response = await client.get(f"/esignatures/{esignature.id}", headers=admin["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(esignature.id)
    assert body["company_id"] == str(company_id)
    assert body["signer_name"] == "Jane Client"
    assert body["signer_email"] == "jane@client.test"
    assert body["ip_address"] == "203.0.113.42"
    assert body["document_type"] == "change_order"
    assert body["signature_artifact_path"] == f"{company_id}/esignatures/{esignature.id}.png"


async def test_get_esignature_allowed_for_read_roles(client):
    admin = await _register_and_login(client, "Acme Construction", "esig-roles-admin@acme.test")
    company_id = uuid.UUID(admin["company_id"])
    user_id = uuid.UUID(admin["user_id"])

    esignature = await _capture(company_id, user_id)

    pm = await _invite_and_login_as(client, admin, "project_manager", "esig-pm@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "esig-acct@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "esig-client@acme.test")

    for actor in (admin, pm, accountant, client_role):
        response = await client.get(f"/esignatures/{esignature.id}", headers=actor["headers"])
        assert response.status_code == 200, response.text


async def test_get_esignature_blocked_for_field_crew(client):
    admin = await _register_and_login(client, "Acme Construction", "esig-crew-admin@acme.test")
    company_id = uuid.UUID(admin["company_id"])
    user_id = uuid.UUID(admin["user_id"])

    esignature = await _capture(company_id, user_id)

    field_crew = await _invite_and_login_as(client, admin, "field_crew", "esig-crew@acme.test")

    response = await client.get(f"/esignatures/{esignature.id}", headers=field_crew["headers"])
    assert response.status_code == 403


async def test_get_esignature_nonexistent_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "esig-404-admin@acme.test")

    response = await client.get(
        "/esignatures/00000000-0000-0000-0000-000000000000", headers=admin["headers"]
    )
    assert response.status_code == 404


async def test_get_esignature_cross_tenant_returns_404(client):
    a = await _register_and_login(client, "Company A", "esig-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "esig-cross-b@acme.test")
    company_a_id = uuid.UUID(a["company_id"])
    user_a_id = uuid.UUID(a["user_id"])

    esignature = await _capture(company_a_id, user_a_id)

    response = await client.get(f"/esignatures/{esignature.id}", headers=b["headers"])
    assert response.status_code == 404
