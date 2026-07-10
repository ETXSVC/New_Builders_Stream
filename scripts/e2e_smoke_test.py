"""Full-stack E2E regression check. Run against a live `docker compose up -d`
stack — hits real HTTP ports, not the in-process ASGI transport the pytest
suite uses. See docs/superpowers/plans/2026-07-07-phase-0-foundation.md,
Task 19 for the original checks, and
docs/superpowers/plans/2026-07-08-phase-1-crm-project-management.md,
Task 19 for the Lead-to-Won-to-Project exit-criterion block added later
in this file."""

import re
import sys
import time
import uuid

import httpx

BACKEND_URL = "http://localhost:8000"
FRONTEND_URL = "http://localhost:3000"
PASSWORD = "supersecret123"
# Test emails use the "e2e.example" domain, not "e2e.test". pydantic's EmailStr
# calls email_validator.validate_email() with no way to pass test_environment=True,
# so by default it rejects RFC 2606 reserved-use TLDs. The pytest suite works
# around this with a process-local monkeypatch (see backend/tests/conftest.py's
# SPECIAL_USE_DOMAIN_NAMES comment), but that workaround is pytest-process-only —
# it does not apply to the real backend container this script talks to over real
# HTTP. Discovered when this script was first run against the live stack in Task
# 19: "@e2e.test" is rejected with a 422 by the real, deployed backend even though
# the equivalent in-process pytest fixtures (which use "acme.test") pass, because
# they benefit from the conftest.py workaround. ".example" is also an RFC
# 2606 reserved-for-documentation TLD (so it's just as clearly non-production),
# but it is not on this project's installed email_validator version's
# SPECIAL_USE_DOMAIN_NAMES denylist (confirmed: {'arpa', 'invalid', 'local',
# 'localhost', 'onion', 'test'}), so it validates successfully.


def wait_for_backend(client: httpx.Client, timeout_seconds: int = 30) -> None:
    deadline = time.time() + timeout_seconds
    last_error = None
    while time.time() < deadline:
        try:
            response = client.get("/health")
            if response.status_code == 200 and response.json() == {"status": "ok"}:
                return
        except httpx.ConnectError as exc:
            last_error = exc
        time.sleep(1)
    raise RuntimeError(f"Backend never became healthy within {timeout_seconds}s: {last_error}")


def register(client: httpx.Client, company_name: str, email: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "company_name": company_name,
            "admin_full_name": "E2E Admin",
            "admin_email": email,
            "admin_password": PASSWORD,
        },
    )
    assert response.status_code == 201, f"register failed: {response.status_code} {response.text}"
    return response.json()


def login(client: httpx.Client, email: str) -> dict:
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, f"login failed: {response.status_code} {response.text}"
    return response.json()


def run() -> None:
    run_id = uuid.uuid4().hex[:8]  # unique suffix so repeated runs don't collide on email uniqueness
    checks_passed = []

    with httpx.Client(base_url=BACKEND_URL, timeout=10.0) as client:
        wait_for_backend(client)
        checks_passed.append("backend /health reachable over real HTTP")

        company_a = register(client, "E2E Company A", f"admin-a-{run_id}@e2e.example")
        token_a = login(client, f"admin-a-{run_id}@e2e.example")["access_token"]
        headers_a = {"Authorization": f"Bearer {token_a}"}
        checks_passed.append("company A registered and logged in over real HTTP")

        own_company = client.get(f"/companies/{company_a['company_id']}", headers=headers_a)
        assert own_company.status_code == 200, own_company.text
        checks_passed.append("company A can read its own company record")

        company_b = register(client, "E2E Company B", f"admin-b-{run_id}@e2e.example")

        cross_tenant = client.get(f"/companies/{company_b['company_id']}", headers=headers_a)
        assert cross_tenant.status_code == 404, (
            f"CRITICAL: cross-tenant isolation failed over real network — expected 404, "
            f"got {cross_tenant.status_code}: {cross_tenant.text}"
        )
        checks_passed.append("cross-tenant isolation holds over real HTTP (company A cannot read company B)")

        child = client.post(
            f"/companies/{company_a['company_id']}/children",
            json={"name": "E2E Branch"},
            headers=headers_a,
        )
        assert child.status_code == 201, child.text
        child_id = child.json()["id"]
        checks_passed.append("nested child-company creation works over real HTTP")

        child_read = client.get(f"/companies/{child_id}", headers=headers_a)
        assert child_read.status_code == 200 and child_read.json()["parent_id"] == company_a["company_id"]
        checks_passed.append("parent can read its own newly-created child branch")

        invite_email = f"invitee-{run_id}@e2e.example"
        invite = client.post(
            "/invitations", json={"email": invite_email, "role": "field_crew"}, headers=headers_a
        )
        assert invite.status_code == 201, invite.text
        checks_passed.append("invitation created over real HTTP")

        accept = client.post(
            f"/invitations/{invite.json()['id']}/accept",
            json={"full_name": "E2E Invitee", "password": PASSWORD},
        )
        assert accept.status_code == 200, accept.text
        checks_passed.append("invitation accepted over real HTTP")

        invitee_login = login(client, invite_email)
        assert invitee_login["default_company_id"] == company_a["company_id"]
        checks_passed.append("newly-invited user can log in and lands in the correct company")

        # --- Phase 1 exit criterion: Lead -> Won -> Project, over real HTTP ---
        # Fresh company so this block is self-contained and independent of the
        # Phase 0 checks above.
        company_c = register(client, "E2E Company C", f"admin-c-{run_id}@e2e.example")
        token_c = login(client, f"admin-c-{run_id}@e2e.example")["access_token"]
        headers_c = {"Authorization": f"Bearer {token_c}"}
        checks_passed.append("company C (Lead-to-Project flow) registered and logged in over real HTTP")

        project_name = f"E2E Kitchen Remodel {run_id}"
        lead = client.post(
            "/leads",
            json={
                "contact_name": "E2E Lead Contact",
                "project_name": project_name,
                "email": f"lead-{run_id}@e2e.example",
                "project_type": "remodel",
            },
            headers=headers_c,
        )
        assert lead.status_code == 201, f"lead create failed: {lead.status_code} {lead.text}"
        lead_id = lead.json()["id"]
        assert lead.json()["status"] == "new", f"expected initial status='new', got {lead.json()['status']!r}"
        checks_passed.append("Lead created over real HTTP (status=new)")

        # Walk the FULL legal transition chain (app/services/lead_transitions.py's
        # LEAD_TRANSITIONS table: new -> contacted -> estimating -> qualified -> won).
        # Driving every intermediate PATCH for real confirms the deployed backend
        # enforces the same state machine the in-process tests exercise — skipping
        # straight to "won" would not prove that.
        for next_status in ("contacted", "estimating", "qualified", "won"):
            transition = client.patch(
                f"/leads/{lead_id}", json={"status": next_status}, headers=headers_c
            )
            assert transition.status_code == 200, (
                f"legal Lead transition to {next_status!r} was rejected over real HTTP: "
                f"{transition.status_code} {transition.text}"
            )
            assert transition.json()["status"] == next_status, (
                f"expected status={next_status!r} after transition, got "
                f"{transition.json()['status']!r}"
            )
            checks_passed.append(f"Lead legally transitioned to status={next_status!r} over real HTTP")

        # LEAD_WON's side effect (Task 1.18): a draft Project should now exist,
        # referencing this Lead, carrying over project_name, with site_address=''.
        # ProjectResponse (admin/PM shape) exposes lead_id, but there's no
        # "get project by lead_id" route, so list and filter client-side.
        projects = client.get("/projects", headers=headers_c)
        assert projects.status_code == 200, projects.text
        matching_projects = [
            p for p in projects.json()["items"] if p.get("lead_id") == lead_id
        ]
        assert len(matching_projects) == 1, (
            f"expected exactly one drafted Project referencing lead_id={lead_id}, "
            f"found {len(matching_projects)}: {projects.json()['items']}"
        )
        drafted_project = matching_projects[0]
        assert drafted_project["name"] == project_name, (
            f"drafted Project name {drafted_project['name']!r} != Lead's "
            f"project_name {project_name!r}"
        )
        assert drafted_project["site_address"] == "", (
            f"drafted Project site_address expected '', got "
            f"{drafted_project['site_address']!r}"
        )
        checks_passed.append(
            "winning a Lead over real HTTP drafts a Project carrying over "
            "project_name with site_address=''"
        )

        # A client-role user hitting the same GET /projects/{id} route gets the
        # sanitized ProjectClientDashboardResponse shape (design decision #8),
        # which has no lead_id field at all — assert the key is entirely absent,
        # not just null.
        client_email = f"client-{run_id}@e2e.example"
        client_invite = client.post(
            "/invitations", json={"email": client_email, "role": "client"}, headers=headers_c
        )
        assert client_invite.status_code == 201, client_invite.text
        client_accept = client.post(
            f"/invitations/{client_invite.json()['id']}/accept",
            json={"full_name": "E2E Client", "password": PASSWORD},
        )
        assert client_accept.status_code == 200, client_accept.text
        client_token = login(client, client_email)["access_token"]
        client_headers = {"Authorization": f"Bearer {client_token}"}
        checks_passed.append("client-role user invited and accepted over real HTTP")

        client_project_view = client.get(
            f"/projects/{drafted_project['id']}", headers=client_headers
        )
        assert client_project_view.status_code == 200, client_project_view.text
        client_project_body = client_project_view.json()
        assert "lead_id" not in client_project_body, (
            f"client-facing GET /projects/{{id}} leaked lead_id: {client_project_body}"
        )
        assert client_project_body["site_address"] == "", (
            f"expected client-facing site_address to be '', got "
            f"{client_project_body['site_address']!r}"
        )
        checks_passed.append(
            "client-role GET /projects/{id} returns sanitized dashboard shape "
            "over real HTTP with no lead_id key present"
        )

    with httpx.Client(timeout=10.0) as client:
        frontend_response = client.get(FRONTEND_URL)
        assert frontend_response.status_code == 200, frontend_response.text
        # Next.js 16 / React 19 SSR inserts an HTML comment marker between static
        # and dynamic text segments in a Server Component (design decision #10),
        # so the raw body is literally "Backend status: <!-- -->ok", not a
        # contiguous string — strip comments before asserting so this doesn't
        # false-fail on correct output.
        rendered_text = re.sub(r"<!--.*?-->", "", frontend_response.text)
        assert "Backend status: ok" in rendered_text, (
            f"Frontend did not report backend as healthy. Body: {frontend_response.text[:500]}"
        )
        checks_passed.append("frontend container reaches backend container over the Docker network and renders it")

    print(f"\n{'=' * 60}\nE2E SMOKE TEST: {len(checks_passed)}/{len(checks_passed)} checks passed\n{'=' * 60}")
    for check in checks_passed:
        print(f"  PASS: {check}")


if __name__ == "__main__":
    try:
        run()
    except AssertionError as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - top-level smoke test, want any failure to exit non-zero with context
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
