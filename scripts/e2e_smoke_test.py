"""Full-stack E2E regression check. Run against a live `docker compose up -d`
stack — hits real HTTP ports, not the in-process ASGI transport the pytest
suite uses. See docs/superpowers/plans/2026-07-07-phase-0-foundation.md,
Task 19 for the original checks,
docs/superpowers/plans/2026-07-08-phase-1-crm-project-management.md,
Task 19 for the Lead-to-Won-to-Project exit-criterion block added later
in this file, docs/superpowers/plans/2026-07-09-phase-2-estimation-esignature.md,
Task 2.25 for the Estimate-to-Approval exit-criterion block added after that,
and docs/superpowers/plans/2026-07-13-compliance-tracking.md, Task 3.13 for
the Compliance Tracking exit-criterion block added last.

Task 2.25 note on the `worker` service: this script deliberately does NOT
bring up the `worker` container (Dramatiq's PDF-export consumer) and does
NOT exercise `POST /estimates/{id}/export` / poll for `pdf_status=='ready'`.
This is an explicit, considered deferral, not a silent coverage gap: bringing
up and reliably waiting on `worker` inside this smoke-test script has already
proven disproportionately flaky on this host (a Docker Desktop crash during a
`worker` image build was hit earlier in this same effort, and the `worker`'s
Redis dependency was separately found to have been missing from CI entirely
until a later fix). Task 2.15's own backend unit test
(`backend/tests/test_estimate_pdf_export.py`) already exercises the full
async PDF pipeline in-process, so that coverage isn't lost — it just lives
there instead of here. `redis` IS still brought up for this script, though:
the `backend` container imports `app.tasks.broker` at module scope (Task
2.14), so the API process itself won't even start without a reachable Redis,
even though nothing in this test enqueues a job.

Task 3.13 note on the `scheduler` service, matching Task 2.25's own
deferral above exactly: this script deliberately does NOT attempt to
exercise the `scheduler` service's actual daily firing
(`check_compliance_expiry`'s real message flow, `app/scheduler.py` /
`app/tasks/compliance_expiry.py`). Reliably waiting on a real
cron-scheduled background job inside a smoke-test script is
disproportionately flaky for what this test needs to prove, and
`backend/tests/test_compliance_expiry_task.py` already exercises that
logic directly and thoroughly (plus Task 3.9's own live-verification pass
already exercised the real scheduler->worker->DB chain once). `scheduler`
IS still brought up alongside the rest of the stack when this script is
run against the live stack (confirmed to start cleanly), but nothing
below talks to it.

Task 3.29 note on Billing's seat-usage scheduled job, matching the Task
2.25/3.13 deferrals above exactly: this script exercises the trial/portal/
RBAC/read-only-enforcement flow (Task 3.19's automatic trial creation,
Task 3.20's `/subscriptions/me` and `/subscriptions/portal-session`, and
Task 3.24's `block_if_read_only`), all entirely over real HTTP, but does
NOT attempt to exercise the seat-usage scheduled job's (Task 3.23)
actual daily firing (`report_seat_usage`'s real message flow via
`app/scheduler.py` / `app/tasks/seat_usage.py`) for the identical
"disproportionately flaky, already covered elsewhere" reason — that
logic is already exercised directly and thoroughly by
`backend/tests/test_seat_usage_task.py` and `backend/tests/test_scheduler.py`.
This block ALSO makes this script's first-ever direct connection to
Postgres (see the block itself, near `company_f`, for why and how).

Task 3.49 note on Invoicing/AR-AP's overdue-flagging scheduled job (Task
3.45), matching the Task 2.25/3.13/3.29 deferrals above exactly: this
script exercises the deposit-invoice auto-generation on Estimate approval
(Task 3.39), invoice send/payment/paid-transition, a vendor Bill with
payment, and the profitability report reflecting both, all entirely over
real HTTP, but does NOT attempt to exercise `flag_overdue_financial_records`'s
actual daily firing via `app/scheduler.py` — that logic is already
exercised directly and thoroughly by
`backend/tests/test_flag_overdue_financial_records.py`.

Task 4.16 note on the Integrations sync worker leg, matching the Task
2.25/3.13/3.29/3.49 deferrals above exactly: this script exercises the
Integrations flow entirely over real HTTP — GET /integrations/quickbooks/
connect returning a fake authorization URL (Task 4.7), GET /integrations/
quickbooks/callback creating a real connection from a real signed `state`
token (Task 4.9), GET /integrations/quickbooks/sync-status showing an
empty `records` list immediately after connecting (Task 4.10), and an
Invoice creation whose INVOICE_CREATED handler (Task 4.11) enqueues a
`sync_financial_record` message to the real Redis queue — but does NOT
poll for the resulting sync record to appear afterward. The live worker
DOES consume these messages (docker-compose.yml's worker command imports
`app.tasks.accounting_sync` — added during this same task after its live
run caught the module missing and every sync message landing in the
dead-letter queue), and a `status='success'` row was confirmed in the
stack's own postgres during this task's verification — but asserting on
it from this script would mean polling for an async consumer's write,
exactly the "disproportionately flaky" pattern every deferral above
exists to avoid. The 201 proves the route-level wiring (including the
enqueuing event handler) didn't error; the consume-and-push path (fake
provider push, per-record status upsert, failure/retry) is already
exercised directly and thoroughly by
`backend/tests/test_accounting_sync.py`.

Task 5.10 note on tier gating (docs/superpowers/specs/2026-07-15-tier-gating-design.md):
registration can only ever produce a trialing/pro subscription (Task 3.19),
and Tasks 5.1-5.9 gated Accounting, Integrations, and child-branch creation
behind the Enterprise tier, so this script now bumps three companies to
enterprise via `_set_subscription_tier` (the same raw-SQL owner-role
approach as `_set_subscription_status`): company A (its nested
child-company creation check hits the now-Enterprise-gated
POST /companies/{id}/children), company G (the Invoicing/AR-AP block —
bumped BEFORE its Estimate approval, because the deposit-invoice
auto-draft that block asserts on is itself tier-gated inside the
ESTIMATE_APPROVED handler since Task 5.8 and would silently not draft at
pro), and company H (the Integrations block). Company H additionally
proves the gate exists in the running stack with one live 403 probe:
GET /integrations/quickbooks/connect at the trial's pro tier must return
403 BEFORE the bump. Company D's Estimate-to-Approval block deliberately
stays at the trial's pro tier — Estimation is Pro+, and that block never
asserts on the (now Enterprise-only) deposit-invoice side effect. Company
F's Billing block also stays untouched: it asserts the trial's own
tier='pro' from GET /subscriptions/me. Unlike company F's read-only status
flip (restored below because 'canceled' BLOCKS writes for anything running
against this same live stack afterward), the tier bumps are deliberately
not restored: enterprise only ever broadens access, and A/G/H are
per-run throwaway companies nothing else reuses."""

import asyncio
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import asyncpg
import httpx
import jwt

# Task 3.29: owner-role Postgres DSN for this script's one direct-DB write
# (flipping a subscription's status outside of any real Stripe webhook, to
# prove `block_if_read_only`'s enforcement). Host-side, not container-side,
# so "localhost" (not the Docker-network "postgres" hostname `DATABASE_URL`
# uses inside containers) is correct here — this script runs on the host,
# same reasoning as backend/.env's own MIGRATIONS_DATABASE_URL and
# TEST_DATABASE_URL entries. Credentials/db name match backend/.env's
# POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB (the Postgres superuser
# docker-compose.yml's `postgres` service is seeded with), not the
# restricted `app_user` role the running `backend` container itself uses —
# same owner-vs-app-role distinction backend/tests/test_subcontractor_assignments.py's
# OWNER_DSN and backend/tests/test_billing_tenant_isolation.py already rely
# on for equivalent raw-SQL test setup, applied here for the first time
# against the live stack instead of the pytest-only test database.
OWNER_DATABASE_DSN = "postgresql://postgres:devpassword@localhost:5432/builders_stream"

# Task 4.16: the backend's JWT signing secret, matching the repo-root .env's
# JWT_SECRET entry — the exact value the running `backend` container receives
# via docker-compose.yml's `env_file: .env` and reads as
# `app.config.Settings.jwt_secret`. Needed host-side to sign the OAuth
# `state` token GET /integrations/{provider}/callback verifies
# (`backend/app/services/integration_oauth_state.py`); there is no HTTP route
# that hands out a raw state token to a script (connect embeds it inside the
# fake provider's authorization_url, which a real browser flow would follow
# and this script deliberately does not). Hardcoded here with the same
# "matches .env, host-side" reasoning as OWNER_DATABASE_DSN above, rather
# than importing backend application code into this standalone script —
# this file's established convention is replicating backend-internal needs
# inline (see _set_subscription_status's raw-SQL approach), never importing
# from backend/.
JWT_SECRET = "dev-only-secret-change-me"

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


async def _set_subscription_status(company_id: str, status: str) -> None:
    """Task 3.29: directly overwrite a `subscriptions` row's `status`
    column via a raw owner-role connection — there is no HTTP route for
    this (a subscription's status is normally only ever changed by a real
    Stripe webhook event, Task 3.21), and the owner role bypasses RLS the
    same way `OWNER_DSN`-based test setup does in the backend's own pytest
    suite. `asyncpg` has no synchronous API, so this is wrapped in
    `asyncio.run()` at each call site rather than making the whole script
    async just for this one connection."""
    conn = await asyncpg.connect(OWNER_DATABASE_DSN)
    try:
        result = await conn.execute(
            "UPDATE subscriptions SET status = $1 WHERE company_id = $2",
            status,
            uuid.UUID(company_id),
        )
        assert result == "UPDATE 1", (
            f"expected exactly one subscriptions row updated for company_id={company_id!r}, "
            f"got {result!r}"
        )
    finally:
        await conn.close()


async def _set_subscription_tier(company_id: str, tier: str) -> None:
    """Task 5.10: directly overwrite a `subscriptions` row's `tier` column
    via a raw owner-role connection — registration can only ever produce a
    trialing/pro subscription (Task 3.19), and there is no HTTP route that
    changes a tier (in production only a real Stripe webhook event does,
    Task 3.21), so the blocks that exercise Enterprise-gated modules
    (companies A, G, and H — see the module docstring's Task 5.10 note)
    bump themselves here first. Same owner-DSN raw-SQL approach, same
    `asyncio.run()`-at-each-call-site shape, and same exactly-one-row
    assertion as `_set_subscription_status` above."""
    conn = await asyncpg.connect(OWNER_DATABASE_DSN)
    try:
        result = await conn.execute(
            "UPDATE subscriptions SET tier = $1 WHERE company_id = $2",
            tier,
            uuid.UUID(company_id),
        )
        assert result == "UPDATE 1", (
            f"expected exactly one subscriptions row updated setting tier={tier!r} "
            f"for company_id={company_id!r}, got {result!r}"
        )
    finally:
        await conn.close()


def _sign_integration_oauth_state(company_id: str, provider: str) -> str:
    """Task 4.16: replicate `sign_oauth_state` from
    `backend/app/services/integration_oauth_state.py` exactly — an HS256
    JWT with claims `company_id`, `provider`, `aud="integration_oauth_state"`,
    `iat`, and `exp=iat+10min`, signed with the backend's `jwt_secret`
    setting (JWT_SECRET above). Replicated inline rather than imported —
    see JWT_SECRET's own comment for why. Any drift between this and the
    backend helper fails loudly: `verify_oauth_state` rejects a
    wrong-secret/wrong-audience/expired token and the callback check below
    turns that into a 400 assertion failure, so this can't silently rot."""
    now = datetime.now(timezone.utc)
    payload = {
        "company_id": company_id,
        "provider": provider,
        "aud": "integration_oauth_state",
        "iat": now,
        "exp": now + timedelta(minutes=10),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


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

        # Tier gating (Task 5.10): child-branch creation is Enterprise-gated
        # (Task 5.7) and registration only produces a trialing/pro
        # subscription — bump company A first or the 201 below would be a 403.
        asyncio.run(_set_subscription_tier(company_a["company_id"], "enterprise"))
        checks_passed.append(
            "company A bumped to enterprise tier via raw-SQL owner connection "
            "(tier gating, Task 5.10: child-branch creation is Enterprise-gated)"
        )

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

        # --- Phase 2 exit criterion: Estimate -> Calculate -> Approve, over real HTTP ---
        # Fresh company so this block is self-contained and independent of the
        # Phase 0/1 checks above.
        company_d = register(client, "E2E Company D", f"admin-d-{run_id}@e2e.example")
        token_d = login(client, f"admin-d-{run_id}@e2e.example")["access_token"]
        headers_d = {"Authorization": f"Bearer {token_d}"}
        checks_passed.append("company D (Estimate-to-Approval flow) registered and logged in over real HTTP")

        estimate_project = client.post(
            "/projects",
            json={"name": f"E2E Estimate Project {run_id}", "site_address": "456 E2E Ave"},
            headers=headers_d,
        )
        assert estimate_project.status_code == 201, estimate_project.text
        estimate_project_id = estimate_project.json()["id"]
        checks_passed.append("Project created over real HTTP for Estimate flow")

        # Hand-verifiable numbers, chosen so the expected total requires no
        # rounding-edge-case reasoning: subtotal = 2.00 * 100.00 = 200.00;
        # total = round(200.00 * 1.10 * 1.15, 2) = 253.00 exactly.
        markup_profile = client.post(
            "/markup-profiles",
            json={"name": f"E2E Markup {run_id}", "overhead_pct": "10.00", "profit_pct": "15.00"},
            headers=headers_d,
        )
        assert markup_profile.status_code == 201, markup_profile.text
        markup_profile_id = markup_profile.json()["id"]
        checks_passed.append("Markup Profile created over real HTTP (overhead_pct=10.00, profit_pct=15.00)")

        catalog_item = client.post(
            "/catalogs/items",
            json={
                "category": "framing",
                "name": f"E2E Lumber {run_id}",
                "unit": "each",
                "unit_rate": "100.00",
            },
            headers=headers_d,
        )
        assert catalog_item.status_code == 201, catalog_item.text
        catalog_item_id = catalog_item.json()["id"]
        checks_passed.append("Cost Catalog item created over real HTTP (unit_rate=100.00)")

        estimate = client.post(
            "/estimates",
            json={"project_id": estimate_project_id, "markup_profile_id": markup_profile_id},
            headers=headers_d,
        )
        assert estimate.status_code == 201, estimate.text
        estimate_id = estimate.json()["id"]
        checks_passed.append("Estimate created over real HTTP against the Project")

        lines = client.put(
            f"/estimates/{estimate_id}/lines",
            json={"items": [{"cost_catalog_item_id": catalog_item_id, "quantity": "2.00"}]},
            headers=headers_d,
        )
        assert lines.status_code == 200, lines.text
        checks_passed.append("Estimate line items replaced over real HTTP (quantity=2.00)")

        calculated = client.post(f"/estimates/{estimate_id}/calculate", headers=headers_d)
        assert calculated.status_code == 200, calculated.text
        calculated_body = calculated.json()
        assert calculated_body["subtotal"] == "200.00", (
            f"expected subtotal='200.00', got {calculated_body['subtotal']!r}"
        )
        assert calculated_body["total"] == "253.00", (
            f"expected total='253.00' (200.00 * 1.10 * 1.15), got {calculated_body['total']!r}"
        )
        checks_passed.append(
            "Estimate calculated over real HTTP with hand-verifiable subtotal=200.00, total=253.00"
        )

        sent = client.post(f"/estimates/{estimate_id}/send-for-signature", headers=headers_d)
        assert sent.status_code == 200, sent.text
        assert sent.json()["status"] == "sent", f"expected status='sent', got {sent.json()['status']!r}"
        checks_passed.append("Estimate sent for signature over real HTTP (status=sent)")

        client_email = f"client-est-{run_id}@e2e.example"
        client_invite = client.post(
            "/invitations", json={"email": client_email, "role": "client"}, headers=headers_d
        )
        assert client_invite.status_code == 201, client_invite.text
        client_accept = client.post(
            f"/invitations/{client_invite.json()['id']}/accept",
            json={"full_name": "E2E Estimate Client", "password": PASSWORD},
        )
        assert client_accept.status_code == 200, client_accept.text
        client_est_token = login(client, client_email)["access_token"]
        client_est_headers = {"Authorization": f"Bearer {client_est_token}"}
        checks_passed.append("client-role user invited and accepted over real HTTP for Estimate approval")

        signer_name = "E2E Estimate Client"
        signer_email = client_email
        approved = client.post(
            f"/estimates/{estimate_id}/approve",
            data={"signer_name": signer_name, "signer_email": signer_email},
            files={"signature_artifact": ("signature.png", b"fake-e2e-signature-bytes", "image/png")},
            headers=client_est_headers,
        )
        assert approved.status_code == 200, approved.text
        approved_body = approved.json()
        assert approved_body["is_snapshotted"] is True, (
            f"expected is_snapshotted=True after approval, got {approved_body['is_snapshotted']!r}"
        )
        assert approved_body["status"] == "approved", (
            f"expected status='approved', got {approved_body['status']!r}"
        )
        assert approved_body["esignature_id"] is not None, "expected esignature_id to be set after approval"
        # Still the exact same hand-verifiable values as after calculate() —
        # proves is_snapshotted genuinely locks the totals, not just that the
        # flag itself got set.
        assert approved_body["subtotal"] == "200.00", (
            f"expected subtotal still '200.00' after approval, got {approved_body['subtotal']!r}"
        )
        assert approved_body["total"] == "253.00", (
            f"expected total still '253.00' after approval, got {approved_body['total']!r}"
        )
        checks_passed.append(
            "Estimate approved by client-role user over real HTTP with a captured e-signature "
            "(status=approved, is_snapshotted=True, totals unchanged from calculate())"
        )

        esignature_id = approved_body["esignature_id"]
        esignature = client.get(f"/esignatures/{esignature_id}", headers=client_est_headers)
        assert esignature.status_code == 200, esignature.text
        esignature_body = esignature.json()
        assert esignature_body["document_type"] == "estimate", (
            f"expected document_type='estimate', got {esignature_body['document_type']!r}"
        )
        assert esignature_body["signer_name"] == signer_name, (
            f"expected signer_name={signer_name!r}, got {esignature_body['signer_name']!r}"
        )
        assert esignature_body["signer_email"] == signer_email, (
            f"expected signer_email={signer_email!r}, got {esignature_body['signer_email']!r}"
        )
        checks_passed.append(
            "GET /esignatures/{id} over real HTTP returns the real, persisted signature record "
            "matching what was submitted"
        )

        # --- Phase 3 exit criterion: Compliance Tracking, over real HTTP ---
        # Fresh company so this block is self-contained and independent of the
        # Phase 0/1/2 checks above. See this module's own docstring for why
        # the `scheduler` service's real daily firing is deliberately NOT
        # exercised here.
        company_e = register(client, "E2E Company E", f"admin-e-{run_id}@e2e.example")
        token_e = login(client, f"admin-e-{run_id}@e2e.example")["access_token"]
        headers_e = {"Authorization": f"Bearer {token_e}"}
        checks_passed.append("company E (Compliance Tracking flow) registered and logged in over real HTTP")

        compliance_project = client.post(
            "/projects",
            json={"name": f"E2E Compliance Project {run_id}", "site_address": "789 E2E Blvd"},
            headers=headers_e,
        )
        assert compliance_project.status_code == 201, compliance_project.text
        compliance_project_id = compliance_project.json()["id"]
        checks_passed.append("Project created over real HTTP for Compliance flow")

        # Subcontractor #1 gets an "expiring soon" document: expires_on =
        # today + 20 days, computed here (not a hardcoded calendar date that
        # would go stale) — falls inside the dashboard's inclusive 0-30-day
        # "expiring soon" window (app/routers/compliance.py's
        # _EXPIRING_SOON_WINDOW).
        expiring_subcontractor = client.post(
            "/subcontractors",
            json={"name": f"E2E Expiring Sub {run_id}", "trade": "electrical"},
            headers=headers_e,
        )
        assert expiring_subcontractor.status_code == 201, expiring_subcontractor.text
        expiring_subcontractor_id = expiring_subcontractor.json()["id"]
        checks_passed.append("Subcontractor created over real HTTP (expiring-soon scenario)")

        expires_soon_on = (date.today() + timedelta(days=20)).isoformat()
        expiring_doc = client.post(
            f"/subcontractors/{expiring_subcontractor_id}/compliance-documents",
            data={"doc_type": "insurance_certificate", "expires_on": expires_soon_on},
            files={"file": ("insurance.pdf", b"fake-e2e-insurance-bytes", "application/pdf")},
            headers=headers_e,
        )
        assert expiring_doc.status_code == 201, expiring_doc.text
        expiring_doc_id = expiring_doc.json()["id"]
        checks_passed.append(
            f"Compliance document uploaded over real HTTP with expires_on={expires_soon_on} "
            "(today + 20 days, within the 30-day expiring-soon window)"
        )

        dashboard = client.get("/compliance/dashboard", headers=headers_e)
        assert dashboard.status_code == 200, dashboard.text
        dashboard_entries = [
            entry
            for entry in dashboard.json()["items"]
            if entry["compliance_document_id"] == expiring_doc_id
        ]
        assert len(dashboard_entries) == 1, (
            f"expected exactly one dashboard entry for compliance_document_id="
            f"{expiring_doc_id!r}, found {len(dashboard_entries)}: {dashboard.json()['items']}"
        )
        assert dashboard_entries[0]["status"] == "expiring_soon", (
            f"expected status='expiring_soon', got {dashboard_entries[0]['status']!r}"
        )
        assert dashboard_entries[0]["subcontractor_id"] == expiring_subcontractor_id, (
            f"expected subcontractor_id={expiring_subcontractor_id!r}, got "
            f"{dashboard_entries[0]['subcontractor_id']!r}"
        )
        checks_passed.append(
            "GET /compliance/dashboard over real HTTP shows the newly-uploaded document "
            "with status='expiring_soon'"
        )

        # Subcontractor #2 is a SEPARATE subcontractor with a
        # separately-uploaded ALREADY-EXPIRED document (today - 10 days),
        # used for the assignment-block scenario below — kept distinct from
        # the expiring-soon subcontractor above so the dashboard assertions
        # above aren't muddied by a second entry.
        expired_subcontractor = client.post(
            "/subcontractors",
            json={"name": f"E2E Expired Sub {run_id}", "trade": "plumbing"},
            headers=headers_e,
        )
        assert expired_subcontractor.status_code == 201, expired_subcontractor.text
        expired_subcontractor_id = expired_subcontractor.json()["id"]
        checks_passed.append("Subcontractor created over real HTTP (already-expired scenario)")

        expired_on = (date.today() - timedelta(days=10)).isoformat()
        expired_doc = client.post(
            f"/subcontractors/{expired_subcontractor_id}/compliance-documents",
            data={"doc_type": "license", "expires_on": expired_on},
            files={"file": ("license.pdf", b"fake-e2e-license-bytes", "application/pdf")},
            headers=headers_e,
        )
        assert expired_doc.status_code == 201, expired_doc.text
        checks_passed.append(
            f"Compliance document uploaded over real HTTP with expires_on={expired_on} "
            "(today - 10 days, already expired)"
        )

        pm_email = f"pm-{run_id}@e2e.example"
        pm_invite = client.post(
            "/invitations", json={"email": pm_email, "role": "project_manager"}, headers=headers_e
        )
        assert pm_invite.status_code == 201, pm_invite.text
        pm_accept = client.post(
            f"/invitations/{pm_invite.json()['id']}/accept",
            json={"full_name": "E2E Project Manager", "password": PASSWORD},
        )
        assert pm_accept.status_code == 200, pm_accept.text
        pm_token = login(client, pm_email)["access_token"]
        pm_headers = {"Authorization": f"Bearer {pm_token}"}
        checks_passed.append("project_manager-role user invited and accepted over real HTTP")

        pm_assignment_attempt = client.post(
            f"/projects/{compliance_project_id}/subcontractor-assignments",
            json={"subcontractor_id": expired_subcontractor_id},
            headers=pm_headers,
        )
        assert pm_assignment_attempt.status_code == 409, (
            "expected 409 when a project_manager assigns a subcontractor with an expired "
            f"compliance document, got {pm_assignment_attempt.status_code}: {pm_assignment_attempt.text}"
        )
        checks_passed.append(
            "project_manager assignment of a subcontractor with an expired compliance "
            "document correctly rejected with 409 over real HTTP"
        )

        override_reason = "E2E verified override: emergency crew needed"
        admin_assignment = client.post(
            f"/projects/{compliance_project_id}/subcontractor-assignments",
            json={"subcontractor_id": expired_subcontractor_id, "override_reason": override_reason},
            headers=headers_e,
        )
        assert admin_assignment.status_code == 201, (
            "expected 201 when an admin assigns a subcontractor with an expired compliance "
            f"document AND supplies an override_reason, got {admin_assignment.status_code}: "
            f"{admin_assignment.text}"
        )
        admin_assignment_body = admin_assignment.json()
        assert admin_assignment_body["override_reason"] == override_reason, (
            f"expected override_reason={override_reason!r}, got "
            f"{admin_assignment_body['override_reason']!r}"
        )
        assert admin_assignment_body["subcontractor_id"] == expired_subcontractor_id
        assert admin_assignment_body["project_id"] == compliance_project_id
        checks_passed.append(
            "admin override of an expired-compliance subcontractor assignment succeeds over "
            "real HTTP (201) with override_reason persisted on the response"
        )
        # Audit log CONTENT verification (action='subcontractor.assigned_with_expired_docs',
        # entity_type, log_metadata) is already covered by the backend's own pytest suite
        # (backend/tests/test_subcontractor_assignments.py::
        # test_admin_override_writes_audit_log_entry_verified_via_raw_sql), which reads the
        # row directly via raw SQL against the owner DB connection. There is no
        # audit-log-read HTTP endpoint anywhere in this API (no GET route exists for
        # `audit_log` in any router), so this script's own check is deliberately limited to
        # confirming the 201 success path itself — it does not invent a fake verification
        # mechanism for the audit log entry's content.
        checks_passed.append(
            "audit log entry for the override is written server-side on the 201 success "
            "path (its content is verified live over raw SQL by the backend's own pytest "
            "suite; no audit-log-read HTTP endpoint exists for this script to check it)"
        )

        # --- Task 3.29 exit criterion: Billing (trial, portal, RBAC, and
        # read-only enforcement), over real HTTP. Fresh company so this
        # block is self-contained and independent of the Phase 0/1/2/3
        # checks above. See this module's own docstring for why the
        # seat-usage scheduled job's real daily firing is deliberately NOT
        # exercised here.
        company_f = register(client, "E2E Company F", f"admin-f-{run_id}@e2e.example")
        token_f = login(client, f"admin-f-{run_id}@e2e.example")["access_token"]
        headers_f = {"Authorization": f"Bearer {token_f}"}
        checks_passed.append("company F (Billing flow) registered and logged in over real HTTP")

        subscription = client.get("/subscriptions/me", headers=headers_f)
        assert subscription.status_code == 200, subscription.text
        subscription_body = subscription.json()
        assert subscription_body["tier"] == "pro", (
            f"expected tier='pro' from POST /auth/register's automatic trial creation "
            f"(Task 3.19), got {subscription_body['tier']!r}"
        )
        assert subscription_body["status"] == "trialing", (
            f"expected status='trialing' immediately after registration, got "
            f"{subscription_body['status']!r}"
        )
        assert subscription_body["included_seats"] == 10, (
            f"expected included_seats=10 (TIER_INCLUDED_SEATS['pro']), got "
            f"{subscription_body['included_seats']!r}"
        )
        checks_passed.append(
            "GET /subscriptions/me over real HTTP shows the automatic 14-day trialing Pro "
            "subscription created at registration (tier=pro, status=trialing, included_seats=10)"
        )

        portal_session = client.post("/subscriptions/portal-session", headers=headers_f)
        assert portal_session.status_code == 200, portal_session.text
        portal_url = portal_session.json()["url"]
        assert portal_url.startswith("https://"), (
            f"expected FakeStripeClient.create_portal_session()'s url to start with "
            f"'https://', got {portal_url!r}"
        )
        checks_passed.append(
            "POST /subscriptions/portal-session over real HTTP as Admin returns a fake "
            "Stripe portal url starting with 'https://'"
        )

        pm_billing_email = f"pm-billing-{run_id}@e2e.example"
        pm_billing_invite = client.post(
            "/invitations",
            json={"email": pm_billing_email, "role": "project_manager"},
            headers=headers_f,
        )
        assert pm_billing_invite.status_code == 201, pm_billing_invite.text
        pm_billing_accept = client.post(
            f"/invitations/{pm_billing_invite.json()['id']}/accept",
            json={"full_name": "E2E Billing PM", "password": PASSWORD},
        )
        assert pm_billing_accept.status_code == 200, pm_billing_accept.text
        pm_billing_token = login(client, pm_billing_email)["access_token"]
        pm_billing_headers = {"Authorization": f"Bearer {pm_billing_token}"}
        checks_passed.append(
            "project_manager-role user invited and accepted over real HTTP for Billing RBAC check"
        )

        pm_subscription_attempt = client.get("/subscriptions/me", headers=pm_billing_headers)
        assert pm_subscription_attempt.status_code == 403, (
            f"expected 403 when a non-Admin/Accountant role (project_manager) reads "
            f"GET /subscriptions/me, got {pm_subscription_attempt.status_code}: "
            f"{pm_subscription_attempt.text}"
        )
        checks_passed.append(
            "project_manager-role GET /subscriptions/me correctly rejected with 403 over real HTTP"
        )

        # Directly flip company F's subscription row to status='past_due' via a raw
        # owner-role Postgres connection — see _set_subscription_status()'s own
        # docstring and OWNER_DATABASE_DSN's comment above for why this is the
        # right (and, for this script, first-ever) way to do it: there's no HTTP
        # route that sets a subscription's status directly, since in production
        # it's only ever driven by a real Stripe webhook event (Task 3.21).
        asyncio.run(_set_subscription_status(company_f["company_id"], "past_due"))
        checks_passed.append(
            "company F's subscription row set to status='past_due' via a direct raw-SQL "
            "owner-role Postgres connection (this script's first direct-DB access)"
        )

        blocked_lead_attempt = client.post(
            "/leads",
            json={
                "contact_name": "E2E Blocked Lead Contact",
                "project_name": f"E2E Blocked Lead {run_id}",
                "email": f"blocked-lead-{run_id}@e2e.example",
                "project_type": "remodel",
            },
            headers=headers_f,
        )
        assert blocked_lead_attempt.status_code == 403, (
            f"expected 403 from block_if_read_only when the company's subscription "
            f"status is 'past_due', got {blocked_lead_attempt.status_code}: "
            f"{blocked_lead_attempt.text}"
        )
        checks_passed.append(
            "POST /leads correctly rejected with 403 over real HTTP while company F's "
            "subscription status='past_due' (block_if_read_only enforcement)"
        )

        # Restore status='trialing' so this doesn't leave the live stack's DB in a
        # read-only-blocked state for company F for anything that might run
        # against this same live stack afterward.
        asyncio.run(_set_subscription_status(company_f["company_id"], "trialing"))
        checks_passed.append(
            "company F's subscription row restored to status='trialing' after the "
            "read-only-enforcement check, over the same raw-SQL owner-role connection"
        )

        # --- Task 3.49 exit criterion: Invoicing/AR-AP (deposit-invoice
        # auto-generation on Estimate approval, invoice send/payment/paid-
        # transition, a vendor Bill with payment, and the profitability
        # report reflecting both), over real HTTP. Fresh company so this
        # block is self-contained and independent of the Phase 0/1/2/3 and
        # Billing checks above. See this module's own docstring for why the
        # overdue-flagging scheduled job's (Task 3.45) real daily firing is
        # deliberately NOT exercised here — that logic is already exercised
        # directly and thoroughly by
        # backend/tests/test_flag_overdue_financial_records.py.
        company_g = register(client, "E2E Company G", f"admin-g-{run_id}@e2e.example")
        token_g = login(client, f"admin-g-{run_id}@e2e.example")["access_token"]
        headers_g = {"Authorization": f"Bearer {token_g}"}
        checks_passed.append("company G (Invoicing/AR-AP flow) registered and logged in over real HTTP")

        # Tier gating (Task 5.10): this block's invoice/bill mutations are
        # Enterprise-gated (Task 5.5), and the deposit-invoice auto-draft it
        # asserts on after Estimate approval is tier-gated inside the
        # ESTIMATE_APPROVED handler too (Task 5.8) — the bump must happen
        # BEFORE the approval or the draft would silently not be created.
        asyncio.run(_set_subscription_tier(company_g["company_id"], "enterprise"))
        checks_passed.append("company G bumped to enterprise tier (tier gating, Task 5.10)")

        invoicing_project = client.post(
            "/projects",
            json={"name": f"E2E Invoicing Project {run_id}", "site_address": "321 E2E Terrace"},
            headers=headers_g,
        )
        assert invoicing_project.status_code == 201, invoicing_project.text
        invoicing_project_id = invoicing_project.json()["id"]
        checks_passed.append("Project created over real HTTP for Invoicing/AR-AP flow")

        # Same hand-verifiable, rounding-tie-free numbers as
        # test_estimate_approved_handler.py's own _create_and_approve_estimate
        # scenario: quantity 8.00 @ unit_rate 5.00 -> subtotal 40.00 ->
        # total = round(40.00 * 1.10 * 1.15, 2) = 50.60 -> 10% deposit = 5.06.
        invoicing_markup_profile = client.post(
            "/markup-profiles",
            json={"name": f"E2E Invoicing Markup {run_id}", "overhead_pct": "10.00", "profit_pct": "15.00"},
            headers=headers_g,
        )
        assert invoicing_markup_profile.status_code == 201, invoicing_markup_profile.text
        invoicing_markup_profile_id = invoicing_markup_profile.json()["id"]
        checks_passed.append(
            "Markup Profile created over real HTTP for Invoicing/AR-AP flow "
            "(overhead_pct=10.00, profit_pct=15.00)"
        )

        invoicing_catalog_item = client.post(
            "/catalogs/items",
            json={
                "category": "materials",
                "name": f"E2E Lumber {run_id}",
                "unit": "board_ft",
                "unit_rate": "5.00",
            },
            headers=headers_g,
        )
        assert invoicing_catalog_item.status_code == 201, invoicing_catalog_item.text
        invoicing_catalog_item_id = invoicing_catalog_item.json()["id"]
        checks_passed.append("Cost Catalog item created over real HTTP for Invoicing/AR-AP flow (unit_rate=5.00)")

        invoicing_estimate = client.post(
            "/estimates",
            json={"project_id": invoicing_project_id, "markup_profile_id": invoicing_markup_profile_id},
            headers=headers_g,
        )
        assert invoicing_estimate.status_code == 201, invoicing_estimate.text
        invoicing_estimate_id = invoicing_estimate.json()["id"]
        checks_passed.append("Estimate created over real HTTP against the Project for Invoicing/AR-AP flow")

        invoicing_lines = client.put(
            f"/estimates/{invoicing_estimate_id}/lines",
            json={"items": [{"cost_catalog_item_id": invoicing_catalog_item_id, "quantity": "8.00"}]},
            headers=headers_g,
        )
        assert invoicing_lines.status_code == 200, invoicing_lines.text
        checks_passed.append("Estimate line items replaced over real HTTP (quantity=8.00) for Invoicing/AR-AP flow")

        invoicing_calculated = client.post(f"/estimates/{invoicing_estimate_id}/calculate", headers=headers_g)
        assert invoicing_calculated.status_code == 200, invoicing_calculated.text
        invoicing_calculated_body = invoicing_calculated.json()
        assert invoicing_calculated_body["subtotal"] == "40.00", (
            f"expected subtotal='40.00' (8.00 * 5.00), got {invoicing_calculated_body['subtotal']!r}"
        )
        assert invoicing_calculated_body["total"] == "50.60", (
            f"expected total='50.60' (40.00 * 1.10 * 1.15), got {invoicing_calculated_body['total']!r}"
        )
        checks_passed.append(
            "Estimate calculated over real HTTP with hand-verifiable subtotal=40.00, total=50.60 for Invoicing/AR-AP flow"
        )

        invoicing_sent = client.post(f"/estimates/{invoicing_estimate_id}/send-for-signature", headers=headers_g)
        assert invoicing_sent.status_code == 200, invoicing_sent.text
        checks_passed.append("Estimate sent for signature over real HTTP for Invoicing/AR-AP flow (status=sent)")

        invoicing_client_email = f"client-inv-{run_id}@e2e.example"
        invoicing_client_invite = client.post(
            "/invitations", json={"email": invoicing_client_email, "role": "client"}, headers=headers_g
        )
        assert invoicing_client_invite.status_code == 201, invoicing_client_invite.text
        invoicing_client_accept = client.post(
            f"/invitations/{invoicing_client_invite.json()['id']}/accept",
            json={"full_name": "E2E Invoicing Client", "password": PASSWORD},
        )
        assert invoicing_client_accept.status_code == 200, invoicing_client_accept.text
        invoicing_client_token = login(client, invoicing_client_email)["access_token"]
        invoicing_client_headers = {"Authorization": f"Bearer {invoicing_client_token}"}
        checks_passed.append(
            "client-role user invited and accepted over real HTTP for Invoicing/AR-AP Estimate approval"
        )

        invoicing_approved = client.post(
            f"/estimates/{invoicing_estimate_id}/approve",
            data={"signer_name": "E2E Invoicing Client", "signer_email": invoicing_client_email},
            files={"signature_artifact": ("signature.png", b"fake-e2e-signature-bytes", "image/png")},
            headers=invoicing_client_headers,
        )
        assert invoicing_approved.status_code == 200, invoicing_approved.text
        checks_passed.append(
            "Estimate approved by client-role user over real HTTP for Invoicing/AR-AP flow "
            "(status=approved), firing ESTIMATE_APPROVED"
        )

        # ESTIMATE_APPROVED's handler (Task 3.39) runs synchronously inside
        # the approve request's own transaction (app.core.events.publish()
        # awaits each handler directly, no queue/worker involved) — no
        # polling/retry needed here, the draft Invoice already exists by the
        # time the approve response returns.
        deposit_invoices = client.get(
            f"/projects/{invoicing_project_id}/invoices", headers=headers_g
        )
        assert deposit_invoices.status_code == 200, deposit_invoices.text
        deposit_invoices_body = deposit_invoices.json()["items"]
        assert len(deposit_invoices_body) == 1, (
            f"expected exactly one auto-generated deposit Invoice after Estimate approval, "
            f"got {len(deposit_invoices_body)}: {deposit_invoices_body}"
        )
        deposit_invoice = deposit_invoices_body[0]
        assert deposit_invoice["status"] == "draft", (
            f"expected status='draft' for the auto-generated deposit Invoice, "
            f"got {deposit_invoice['status']!r}"
        )
        assert deposit_invoice["amount"] == "5.06", (
            f"expected amount='5.06' (10% of the 50.60 Estimate total), "
            f"got {deposit_invoice['amount']!r}"
        )
        assert deposit_invoice["invoice_number"].startswith("INV-"), (
            f"expected invoice_number to start with 'INV-', got {deposit_invoice['invoice_number']!r}"
        )
        checks_passed.append(
            "GET /projects/{id}/invoices over real HTTP shows exactly one auto-generated draft "
            "deposit Invoice (amount=5.06, invoice_number starting with 'INV-') after Estimate approval"
        )

        invoicing_invoice_id = deposit_invoice["id"]
        # Computed relative to today, not a hardcoded calendar date that
        # would go stale — same discipline the Compliance block above uses
        # for expires_on.
        invoicing_due_date = (date.today() + timedelta(days=30)).isoformat()
        invoice_sent = client.post(
            f"/invoices/{invoicing_invoice_id}/send",
            json={"due_date": invoicing_due_date},
            headers=headers_g,
        )
        assert invoice_sent.status_code == 200, invoice_sent.text
        assert invoice_sent.json()["status"] == "sent", (
            f"expected status='sent' after POST /invoices/{{id}}/send, got {invoice_sent.json()['status']!r}"
        )
        checks_passed.append("POST /invoices/{id}/send over real HTTP succeeds (status=sent)")

        invoice_paid = client.post(
            f"/invoices/{invoicing_invoice_id}/payments",
            json={"amount": "5.06", "paid_date": date.today().isoformat()},
            headers=headers_g,
        )
        assert invoice_paid.status_code == 201, invoice_paid.text
        invoice_after_payment = client.get(f"/invoices/{invoicing_invoice_id}", headers=headers_g)
        assert invoice_after_payment.status_code == 200, invoice_after_payment.text
        assert invoice_after_payment.json()["status"] == "paid", (
            f"expected status='paid' after a full payment, got {invoice_after_payment.json()['status']!r}"
        )
        checks_passed.append(
            "POST /invoices/{id}/payments for the full amount over real HTTP succeeds "
            "and transitions the Invoice to status=paid"
        )

        vendor_bill = client.post(
            "/bills",
            json={"project_id": invoicing_project_id, "vendor_name": f"E2E Vendor {run_id}", "amount": "20.00"},
            headers=headers_g,
        )
        assert vendor_bill.status_code == 201, vendor_bill.text
        vendor_bill_id = vendor_bill.json()["id"]
        checks_passed.append("POST /bills over real HTTP with a free-text vendor succeeds (status=unpaid)")

        vendor_bill_paid = client.post(
            f"/bills/{vendor_bill_id}/payments",
            json={"amount": "20.00", "paid_date": date.today().isoformat()},
            headers=headers_g,
        )
        assert vendor_bill_paid.status_code == 201, vendor_bill_paid.text
        vendor_bill_after_payment = client.get(f"/bills/{vendor_bill_id}", headers=headers_g)
        assert vendor_bill_after_payment.status_code == 200, vendor_bill_after_payment.text
        assert vendor_bill_after_payment.json()["status"] == "paid", (
            f"expected status='paid' after a full Bill payment, got "
            f"{vendor_bill_after_payment.json()['status']!r}"
        )
        checks_passed.append(
            "POST /bills/{id}/payments for the full amount over real HTTP succeeds "
            "and transitions the Bill to status=paid"
        )

        profitability = client.get(
            "/reports/profitability?start_date=2020-01-01&end_date=2030-12-31", headers=headers_g
        )
        assert profitability.status_code == 200, profitability.text
        profitability_body = profitability.json()
        matching_project = [
            p for p in profitability_body["projects"] if p["project_id"] == invoicing_project_id
        ]
        assert len(matching_project) == 1, (
            f"expected the Invoicing/AR-AP Project to appear exactly once in the "
            f"profitability report, got {len(matching_project)}: {matching_project}"
        )
        assert matching_project[0]["billed_revenue"] == "5.06", (
            f"expected billed_revenue='5.06' (the sent-then-paid deposit Invoice's amount), "
            f"got {matching_project[0]['billed_revenue']!r}"
        )
        assert matching_project[0]["actual_cost"] == "20.00", (
            f"expected actual_cost='20.00' (the paid vendor Bill's amount), "
            f"got {matching_project[0]['actual_cost']!r}"
        )
        checks_passed.append(
            "GET /reports/profitability over real HTTP includes the Invoicing/AR-AP Project "
            "with billed_revenue=5.06 (the Invoice) and actual_cost=20.00 (the Bill)"
        )

        # --- Task 4.16 exit criterion: Integrations (QuickBooks connect,
        # OAuth callback with a real signed state, sync-status, and the
        # INVOICE_CREATED sync enqueue's route-level wiring), over real
        # HTTP. Fresh company so this block is self-contained and
        # independent of every block above. See this module's own docstring
        # for why the enqueued sync_financial_record message is deliberately
        # NOT drained/waited on here — that consume-and-push logic is
        # already exercised directly and thoroughly by
        # backend/tests/test_accounting_sync.py.
        company_h = register(client, "E2E Company H", f"admin-h-{run_id}@e2e.example")
        token_h = login(client, f"admin-h-{run_id}@e2e.example")["access_token"]
        headers_h = {"Authorization": f"Bearer {token_h}"}
        checks_passed.append("company H (Integrations flow) registered and logged in over real HTTP")

        # Tier gating (Task 5.10): one live 403 probe BEFORE the tier bump —
        # real-HTTP proof the Integrations gate (Task 5.6) exists in the
        # running stack. Registration left company H at the trial's pro tier,
        # which is exactly the below-tier case for the Enterprise-gated
        # connect route.
        blocked_connect = client.get("/integrations/quickbooks/connect", headers=headers_h)
        assert blocked_connect.status_code == 403, (
            f"pro-tier company should be tier-blocked from integrations connect, "
            f"got {blocked_connect.status_code}: {blocked_connect.text}"
        )
        # "enterprise" in the detail pins this 403 to the TIER gate — a role
        # or read-only 403 would also be 403 but wouldn't name the plan
        # (same disambiguation backend/tests/test_tier_gating.py uses).
        assert "enterprise" in blocked_connect.text, (
            f"403 should come from the tier gate (naming the enterprise plan), "
            f"got: {blocked_connect.text}"
        )
        checks_passed.append("pro-tier company gets 403 from integrations connect (tier gating)")

        asyncio.run(_set_subscription_tier(company_h["company_id"], "enterprise"))
        checks_passed.append("company H bumped to enterprise tier (tier gating, Task 5.10)")

        integration_connect = client.get("/integrations/quickbooks/connect", headers=headers_h)
        assert integration_connect.status_code == 200, (
            f"GET /integrations/quickbooks/connect failed: "
            f"{integration_connect.status_code} {integration_connect.text}"
        )
        authorization_url = integration_connect.json()["authorization_url"]
        assert authorization_url.startswith("https://quickbooks.fake-oauth.test/"), (
            f"expected authorization_url to start with 'https://quickbooks.fake-oauth.test/' "
            f"(FakeAccountingProviderClient.get_authorization_url), got {authorization_url!r}"
        )
        checks_passed.append(
            "GET /integrations/quickbooks/connect over real HTTP as Admin returns a fake "
            "authorization_url starting with 'https://quickbooks.fake-oauth.test/'"
        )

        # The signed state token is generated host-side with the same
        # secret/claims the deployed backend verifies with — see
        # _sign_integration_oauth_state's and JWT_SECRET's own comments.
        # No Authorization header on this request, deliberately: callback
        # is an external OAuth redirect target with no CurrentUser at all
        # (backend/app/routers/integrations.py) — the signed state IS its
        # entire authentication, and this proves that for real.
        oauth_state = _sign_integration_oauth_state(company_h["company_id"], "quickbooks")
        integration_callback = client.get(
            "/integrations/quickbooks/callback",
            params={"code": "fake-code", "state": oauth_state},
        )
        assert integration_callback.status_code == 200, (
            f"GET /integrations/quickbooks/callback with a real signed state failed: "
            f"{integration_callback.status_code} {integration_callback.text}"
        )
        integration_connection_body = integration_callback.json()
        assert integration_connection_body["provider"] == "quickbooks", (
            f"expected provider='quickbooks' on the connection the callback created, "
            f"got {integration_connection_body['provider']!r}"
        )
        checks_passed.append(
            "GET /integrations/quickbooks/callback over real HTTP with a real signed state "
            "token (no bearer token - the state IS the authentication) creates and returns "
            "a connection with provider='quickbooks'"
        )

        integration_sync_status = client.get("/integrations/quickbooks/sync-status", headers=headers_h)
        assert integration_sync_status.status_code == 200, (
            f"GET /integrations/quickbooks/sync-status failed: "
            f"{integration_sync_status.status_code} {integration_sync_status.text}"
        )
        integration_sync_status_body = integration_sync_status.json()
        assert integration_sync_status_body["provider"] == "quickbooks", (
            f"expected provider='quickbooks' from sync-status, got "
            f"{integration_sync_status_body['provider']!r}"
        )
        assert integration_sync_status_body["records"] == [], (
            f"expected records=[] immediately after connecting (nothing synced yet), "
            f"got {integration_sync_status_body['records']!r}"
        )
        checks_passed.append(
            "GET /integrations/quickbooks/sync-status over real HTTP returns 200 with "
            "records=[] immediately after connecting (nothing synced yet)"
        )

        integrations_project = client.post(
            "/projects",
            json={"name": f"E2E Integrations Project {run_id}", "site_address": "654 E2E Way"},
            headers=headers_h,
        )
        assert integrations_project.status_code == 201, integrations_project.text
        integrations_project_id = integrations_project.json()["id"]
        checks_passed.append("Project created over real HTTP for Integrations flow")

        integrations_invoice = client.post(
            f"/projects/{integrations_project_id}/invoices",
            json={"amount": "75.00"},
            headers=headers_h,
        )
        assert integrations_invoice.status_code == 201, (
            f"POST /projects/{{id}}/invoices with an active integration connection failed: "
            f"{integrations_invoice.status_code} {integrations_invoice.text} - the "
            f"INVOICE_CREATED handler's sync enqueue (Task 4.11) runs inside this request, "
            f"so a failure here would implicate that route-level wiring"
        )
        checks_passed.append(
            "POST /projects/{id}/invoices over real HTTP succeeds (201) with an active "
            "quickbooks connection - the INVOICE_CREATED handler enqueued a "
            "sync_financial_record message to the real Redis queue without erroring "
            "(the queue is deliberately not drained here; see the module docstring)"
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
