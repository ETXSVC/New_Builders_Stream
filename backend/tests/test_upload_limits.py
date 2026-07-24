"""Upload size caps (app/core/uploads.read_upload_limited): every
multipart route must 413 an oversized body instead of the previous bare
`await file.read()` (an unbounded memory + disk vector).

Caps are Settings fields precisely so these tests can shrink them to ~1 KiB
instead of shipping multi-megabyte payloads through CI. Helpers are
imported from the routes' own test files (the established cross-file
pattern test_tier_gating.py uses).
"""
from app.config import settings
from tests.test_documents import _create_project, _register_and_login, _upload
from tests.test_subcontractors import (
    _create_subcontractor,
    _upload_compliance_document,
)
from tests.test_subcontractors import (
    _register_and_login as _register_subcontractor_admin,
)

_CAP = 1024


async def test_project_document_upload_over_cap_is_413(client, monkeypatch):
    monkeypatch.setattr(settings, "max_document_upload_bytes", _CAP)
    admin = await _register_and_login(client, "Upload Cap Co 1", "upload-cap-1@acme.test")
    project_id = await _create_project(client, admin)

    over = await _upload(client, admin, project_id, "big.pdf", b"x" * (_CAP + 1))
    assert over.status_code == 413, over.text

    under = await _upload(client, admin, project_id, "small.pdf", b"x" * _CAP)
    assert under.status_code == 201, under.text


async def test_compliance_document_upload_over_cap_is_413(client, monkeypatch):
    monkeypatch.setattr(settings, "max_document_upload_bytes", _CAP)
    admin = await _register_subcontractor_admin(client, "Upload Cap Co 2", "upload-cap-2@acme.test")
    subcontractor = await _create_subcontractor(client, admin)
    assert subcontractor.status_code == 201, subcontractor.text
    subcontractor_id = subcontractor.json()["id"]

    over = await _upload_compliance_document(
        client, admin, subcontractor_id, content=b"x" * (_CAP + 1)
    )
    assert over.status_code == 413, over.text

    under = await _upload_compliance_document(client, admin, subcontractor_id, content=b"x" * _CAP)
    assert under.status_code == 201, under.text


async def test_signature_artifact_over_cap_is_413(client, monkeypatch):
    """Drives the real estimate flow to the approve step, then submits an
    oversized signature artifact. Reuses test_estimate_approved_handler's
    full-flow helpers up to send-for-signature."""
    from tests.test_estimate_approved_handler import (
        _create_catalog_item,
        _create_markup_profile,
        _create_project as _create_estimate_project,
        _invite_and_login_as,
        _register_and_login as _register_estimate_admin,
    )

    monkeypatch.setattr(settings, "max_signature_upload_bytes", _CAP)
    admin = await _register_estimate_admin(client, "Upload Cap Co 3", "upload-cap-3@example.test")
    client_role = await _invite_and_login_as(client, admin, "client", "upload-cap-3-client@example.test")
    project = await _create_estimate_project(client, admin["headers"])
    markup_profile_id = await _create_markup_profile(client, admin["headers"])
    catalog_item_id = await _create_catalog_item(client, admin["headers"])

    create = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup_profile_id},
        headers=admin["headers"],
    )
    estimate_id = create.json()["id"]
    await client.put(
        f"/estimates/{estimate_id}/lines",
        json={"items": [{"cost_catalog_item_id": catalog_item_id, "quantity": "8.00"}]},
        headers=admin["headers"],
    )
    await client.post(f"/estimates/{estimate_id}/calculate", headers=admin["headers"])
    send = await client.post(
        f"/estimates/{estimate_id}/send-for-signature", headers=admin["headers"]
    )
    assert send.status_code == 200, send.text

    over = await client.post(
        f"/estimates/{estimate_id}/approve",
        data={"signer_name": "Client Signer", "signer_email": "sig@example.test"},
        files={"signature_artifact": ("sig.png", b"x" * (_CAP + 1), "image/png")},
        headers=client_role["headers"],
    )
    assert over.status_code == 413, over.text

    under = await client.post(
        f"/estimates/{estimate_id}/approve",
        data={"signer_name": "Client Signer", "signer_email": "sig@example.test"},
        files={"signature_artifact": ("sig.png", b"x" * _CAP, "image/png")},
        headers=client_role["headers"],
    )
    assert under.status_code == 200, under.text
