"""CRM+PM frontend spec Decision 2 item 1: GET .../documents/{id}/download.
No download route existed before this — documents could be uploaded and
listed but never retrieved."""

from tests.conftest import register_and_login


async def _project(client, admin, name):
    response = await client.post(
        "/projects",
        json={"name": name, "site_address": "4 Main St"},
        headers=admin["headers"],
    )
    return response.json()


async def test_download_round_trips_uploaded_bytes(client):
    admin = await register_and_login(client, "Download Co", "download-admin@acme.test")
    project = await _project(client, admin, "Download Project")

    upload = await client.post(
        f"/projects/{project['id']}/documents",
        files={"file": ("plan.txt", b"blueprint bytes", "text/plain")},
        data={"file_name": "plan.txt"},
        headers=admin["headers"],
    )
    assert upload.status_code == 201, upload.text
    document_id = upload.json()["id"]

    download = await client.get(
        f"/projects/{project['id']}/documents/{document_id}/download",
        headers=admin["headers"],
    )
    assert download.status_code == 200, download.text
    assert download.content == b"blueprint bytes"
    assert "plan.txt" in download.headers["content-disposition"]


async def test_download_404_for_document_of_other_project(client):
    admin = await register_and_login(client, "Download Iso Co", "download-iso@acme.test")
    project_a = await _project(client, admin, "Project A")
    project_b = await _project(client, admin, "Project B")

    upload = await client.post(
        f"/projects/{project_a['id']}/documents",
        files={"file": ("a.txt", b"a", "text/plain")},
        data={"file_name": "a.txt"},
        headers=admin["headers"],
    )
    document_id = upload.json()["id"]

    # Right document id, wrong project in the path — must 404, not leak.
    response = await client.get(
        f"/projects/{project_b['id']}/documents/{document_id}/download",
        headers=admin["headers"],
    )
    assert response.status_code == 404
