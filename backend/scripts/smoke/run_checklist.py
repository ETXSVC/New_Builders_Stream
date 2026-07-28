"""Runbook §4 smoke checklist — items 2, 4, 5, 6, 8 and the app half of 11.

Drives a REAL running stack over HTTP. Not the pytest ASGI transport, and
that distinction is the point: item 6 asks whether `X-Forwarded-For`
survives an actual network hop into uvicorn `--proxy-headers` and lands in
`esignatures.ip_address` as legally-significant ESIGN evidence. An
in-process transport cannot answer that.

    # dev stack (docker compose up && alembic upgrade head)
    python scripts/smoke/run_checklist.py

    # production stack, from the box, against the internal backend
    SMOKE_BASE_URL=http://localhost:8000 python scripts/smoke/run_checklist.py

Reads `MIGRATIONS_DATABASE_URL` to verify two things the API will not tell
you: the tier flip that lets an estimate be created, and the row actually
written to `esignatures`.

WHAT THIS DOES NOT COVER — run these by hand, they need real hardware:
  item 1  production config fail-fast  -> scripts/smoke/check_production_config.py
  item 3  TLS + HSTS through Caddy     -> needs a certificate
  item 7  Redis fail-open              -> needs to stop Redis
  item 9  reboot persistence           -> needs to reboot the box
  item 10 backup + restore drill       -> deploy/backup/restore-drill.sh
  item 11 Prometheus/Grafana targets   -> needs the monitoring containers

It creates real rows (a company, a lead, a project, an estimate, a signed
document). Point it at a stack whose database you are willing to dirty.
"""
import asyncio
import os
import sys
import uuid

import asyncpg
import httpx

BASE = os.environ.get("SMOKE_BASE_URL", "http://localhost:8000")

try:
    OWNER_DSN = os.environ["MIGRATIONS_DATABASE_URL"].replace("+asyncpg", "")
except KeyError:  # pragma: no cover - operator error, not a code path
    raise SystemExit(
        "MIGRATIONS_DATABASE_URL is not set. This script reads it to set the "
        "subscription tier and to read back the esignatures row. Export it, or "
        "run from a shell that has sourced the stack's .env."
    )

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append((label, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    return ok


async def set_tier(company_id: str, tier: str) -> None:
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        result = await conn.execute(
            "UPDATE subscriptions SET tier = $1 WHERE company_id = $2", tier, uuid.UUID(company_id)
        )
        assert result == "UPDATE 1", result
    finally:
        await conn.close()


async def register(c: httpx.AsyncClient, company: str, email: str, tier="enterprise") -> dict:
    reg = await c.post(
        "/auth/register",
        json={
            "company_name": company,
            "admin_full_name": "Smoke Admin",
            "admin_email": email,
            "admin_password": "supersecret123",
        },
    )
    assert reg.status_code == 201, reg.text
    login = await c.post("/auth/login", json={"email": email, "password": "supersecret123"})
    assert login.status_code == 200, login.text
    if tier:
        await set_tier(reg.json()["company_id"], tier)
    return {
        "company_id": reg.json()["company_id"],
        "user_id": reg.json()["user_id"],
        "email": email,
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


async def invite_as(c: httpx.AsyncClient, admin: dict, role: str, email: str) -> dict:
    inv = await c.post("/invitations", json={"email": email, "role": role}, headers=admin["headers"])
    assert inv.status_code == 201, inv.text
    acc = await c.post(
        f"/invitations/{inv.json()['id']}/accept",
        json={"full_name": "Smoke Client", "password": "anothersecret123"},
    )
    assert acc.status_code == 200, acc.text
    login = await c.post("/auth/login", json={"email": email, "password": "anothersecret123"})
    assert login.status_code == 200, login.text
    members = await c.get("/companies/members", headers=admin["headers"])
    user_id = next(m["user_id"] for m in members.json()["items"] if m["email"] == email)
    return {
        "email": email,
        "user_id": user_id,
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=60.0) as c:
        # ---------------- item 2: readiness ---------------- #
        print("\n[item 2] Readiness")
        ready = await c.get("/ready")
        body = ready.json()
        check("/ready returns 200", ready.status_code == 200, str(ready.status_code))
        check("status is 'ready'", body.get("status") == "ready", str(body))
        check("database probe ok", body.get("database") == "ok")
        check("redis probe ok", body.get("redis") == "ok")
        health = await c.get("/health")
        check(
            "/health is static liveness (no dependency keys)",
            health.json() == {"status": "ok"},
            str(health.json()),
        )

        # ---------------- item 4: product flow ---------------- #
        print("\n[item 4] Product flow: register -> lead -> won -> estimate -> PDF -> upload")
        admin = await register(c, "Smoke Construction", f"smoke-admin-{uuid.uuid4().hex[:8]}@example.com")
        check("company registered + admin logged in", bool(admin["company_id"]))

        lead = await c.post(
            "/leads",
            json={
                "contact_name": "Jane Homeowner",
                "project_name": "Kitchen Remodel",
                "email": "jane@example.com",
                "phone": "555-0100",
                "project_type": "residential",
                "estimated_value": "15000.00",
            },
            headers=admin["headers"],
        )
        check("lead created", lead.status_code == 201, lead.text[:120])
        lead_id = lead.json()["id"]

        # The spine is mandatory: app/services/lead_transitions.py rejects
        # new -> won outright, so a stage cannot be skipped on the way to won.
        skip = await c.patch(
            f"/leads/{lead_id}", json={"status": "won"}, headers=admin["headers"]
        )
        check(
            "new -> won is refused with 409 (stages cannot be skipped)",
            skip.status_code == 409,
            f"got {skip.status_code}",
        )
        won = None
        for stage in ("contacted", "estimating", "qualified", "won"):
            won = await c.patch(
                f"/leads/{lead_id}", json={"status": stage}, headers=admin["headers"]
            )
            if won.status_code != 200:
                break
        check("lead walked new -> contacted -> estimating -> qualified -> won",
              won.status_code == 200, won.text[:120])

        # LEAD_WON -> drafts a Project through the in-process event bus.
        projects = await c.get("/projects", headers=admin["headers"])
        drafted = [p for p in projects.json()["items"] if p.get("lead_id") == lead_id]
        check(
            "LEAD_WON auto-drafted a project (event bus wired)",
            len(drafted) == 1,
            f"{len(drafted)} matching projects",
        )

        project = await c.post(
            "/projects",
            json={"name": "Smoke Kitchen", "site_address": "123 Main St"},
            headers=admin["headers"],
        )
        assert project.status_code == 201, project.text
        project_id = project.json()["id"]

        markup = await c.post(
            "/markup-profiles",
            json={"name": "Standard Markup", "overhead_pct": "10.00", "profit_pct": "15.00"},
            headers=admin["headers"],
        )
        assert markup.status_code == 201, markup.text
        item = await c.post(
            "/catalogs/items",
            json={"category": "framing", "name": "2x4 Lumber", "unit": "each", "unit_rate": "45.00"},
            headers=admin["headers"],
        )
        assert item.status_code == 201, item.text

        est = await c.post(
            "/estimates",
            json={"project_id": project_id, "markup_profile_id": markup.json()["id"]},
            headers=admin["headers"],
        )
        check("estimate created", est.status_code == 201, est.text[:120])
        estimate_id = est.json()["id"]

        lines = await c.put(
            f"/estimates/{estimate_id}/lines",
            json={"items": [{"cost_catalog_item_id": item.json()["id"], "quantity": "10.00"}]},
            headers=admin["headers"],
        )
        check("estimate lines set", lines.status_code == 200, lines.text[:120])

        calc = await c.post(f"/estimates/{estimate_id}/calculate", headers=admin["headers"])
        total = calc.json().get("total") if calc.status_code == 200 else None
        # 10 x 45.00 = 450.00 direct, +10% overhead, +15% profit = 569.25
        check(
            "estimate calculated to a non-null total",
            calc.status_code == 200 and total is not None,
            f"total={total}",
        )

        # PDF export is the item that proves the worker + storage volume.
        export = await c.post(f"/estimates/{estimate_id}/export", headers=admin["headers"])
        check("PDF export accepted (202, queued)", export.status_code == 202, export.text[:160])

        pdf_ready, pdf_status = False, "unknown"
        for _ in range(60):
            await asyncio.sleep(1)
            current = await c.get(f"/estimates/{estimate_id}", headers=admin["headers"])
            pdf_status = current.json().get("pdf_status")
            if pdf_status == "ready":
                pdf_ready = True
                break
            if pdf_status == "failed":
                break
        check("worker rendered the PDF (pdf_status=ready)", pdf_ready, f"pdf_status={pdf_status}")

        if pdf_ready:
            dl = await c.get(f"/estimates/{estimate_id}/pdf", headers=admin["headers"])
            check(
                "PDF downloads and is a real PDF",
                dl.status_code == 200 and dl.content[:5] == b"%PDF-",
                f"{dl.status_code}, {dl.content[:8]!r}",
            )

        up = await c.post(
            f"/projects/{project_id}/documents",
            files={"file": ("plan.txt", b"site plan contents", "text/plain")},
            data={"file_name": "plan.txt"},
            headers=admin["headers"],
        )
        check("project document uploaded", up.status_code == 201, up.text[:160])

        # ---------------- item 5: upload cap ---------------- #
        print("\n[item 5] Upload cap returns 413")
        cap = int(os.environ.get("MAX_DOCUMENT_UPLOAD_BYTES", 25 * 1024 * 1024))
        oversized = await c.post(
            f"/projects/{project_id}/documents",
            files={"file": ("big.bin", b"x" * (cap + 1024), "application/octet-stream")},
            data={"file_name": "big.bin"},
            headers=admin["headers"],
        )
        check(
            f"upload over the {cap}-byte cap is rejected with 413",
            oversized.status_code == 413,
            f"got {oversized.status_code}",
        )

        # ---------------- item 6: client IP through the proxy hop ------- #
        print("\n[item 6] X-Forwarded-For reaches the e-signature record")
        client_user = await invite_as(
            c, admin, "client", f"smoke-client-{uuid.uuid4().hex[:8]}@example.com"
        )
        grant = await c.post(
            f"/projects/{project_id}/clients",
            json={"user_id": client_user["user_id"]},
            headers=admin["headers"],
        )
        check("client granted project access (migration 0019)", grant.status_code == 201, grant.text[:120])

        sent = await c.post(
            f"/estimates/{estimate_id}/send-for-signature", headers=admin["headers"]
        )
        check("estimate sent for signature", sent.status_code == 200, sent.text[:120])

        # The whole point: a spoofable-looking header that Caddy would have
        # set, arriving over a real HTTP hop into uvicorn --proxy-headers.
        forwarded_ip = "203.0.113.77"
        approve = await c.post(
            f"/estimates/{estimate_id}/approve",
            data={"signer_name": "Jane Client", "signer_email": client_user["email"]},
            files={"signature_artifact": ("sig.png", b"fake-signature-bytes", "image/png")},
            headers={**client_user["headers"], "X-Forwarded-For": f"{forwarded_ip}, 10.0.0.1"},
        )
        check("client approved the estimate", approve.status_code == 200, approve.text[:200])

        if approve.status_code == 200:
            conn = await asyncpg.connect(OWNER_DSN)
            try:
                row = await conn.fetchrow(
                    "SELECT ip_address, signer_email, signed_by_user_id FROM esignatures "
                    "WHERE id = $1",
                    uuid.UUID(approve.json()["esignature_id"]),
                )
            finally:
                await conn.close()
            check(
                "esignatures.ip_address holds the REAL client IP, not a container address",
                str(row["ip_address"]) == forwarded_ip,
                f"stored {row['ip_address']!r}, expected {forwarded_ip!r}",
            )
            check(
                "signer identity is the authenticated account, not free text",
                row["signer_email"] == client_user["email"]
                and str(row["signed_by_user_id"]) == client_user["user_id"],
            )

        # ---------------- item 8: forged webhook ---------------- #
        print("\n[item 8] Forged Stripe webhook is rejected")
        forged = await c.post(
            "/webhooks/stripe",
            content=b'{"type":"customer.subscription.deleted"}',
            headers={"Stripe-Signature": "t=1,v1=deadbeef", "Content-Type": "application/json"},
        )
        check(
            "forged signature rejected with 4xx",
            400 <= forged.status_code < 500,
            f"got {forged.status_code}",
        )
        unsigned = await c.post(
            "/webhooks/stripe",
            content=b"{}",
            headers={"Content-Type": "application/json"},
        )
        check(
            "unsigned webhook rejected with 4xx",
            400 <= unsigned.status_code < 500,
            f"got {unsigned.status_code}",
        )

        # ---------------- item 11 (app half): /metrics ---------------- #
        print("\n[item 11] /metrics exposition (application half)")
        metrics = await c.get("/metrics")
        text = metrics.text
        check("/metrics returns 200", metrics.status_code == 200)
        check(
            "content type is Prometheus exposition",
            metrics.headers.get("content-type", "").startswith("text/plain"),
            metrics.headers.get("content-type", ""),
        )
        for family in (
            "buildersstream_http_requests_total",
            "buildersstream_http_request_duration_seconds_bucket",
            "buildersstream_db_pool_connections_in_use",
            "buildersstream_db_pool_size",
            "buildersstream_dramatiq_queue_depth",
            "buildersstream_dramatiq_dead_letter_depth",
        ):
            check(f"exports {family}", family in text)

        sample_lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
        check(
            "no series carries a tenant/user/path label",
            not any(
                k in ln
                for ln in sample_lines
                for k in ("company_id=", "tenant=", "tenant_id=", "user_id=", "path=", "url=")
            ),
        )
        check(
            "route labels are templates, not the ids just exercised",
            f'route="/projects/{project_id}"' not in text
            and f'route="/estimates/{estimate_id}"' not in text,
        )
        check(
            "the parameterised routes appear as templates",
            'route="/estimates/{estimate_id}"' in text or 'route="/projects/{project_id}"' in text,
        )
        scrape_fail = [
            ln
            for ln in sample_lines
            if ln.startswith("buildersstream_dramatiq_queue_scrape_failures_total ")
        ]
        check(
            "queue-depth probe reached Redis (0 scrape failures)",
            bool(scrape_fail) and scrape_fail[0].split()[-1] in ("0.0", "0"),
            scrape_fail[0] if scrape_fail else "counter missing",
        )
        # A drained queue's Redis key is deleted, so an idle stack legitimately
        # exports no depth series. Prove the namespace SCAN works by planting a
        # queue the worker will never consume, rather than racing it.
        import redis as _redis

        probe = _redis.Redis.from_url(os.environ["REDIS_URL"])
        probe.delete("dramatiq:smoke-probe")
        probe.rpush("dramatiq:smoke-probe", "a", "b", "c")
        try:
            planted = (await c.get("/metrics")).text
            check(
                "queue-depth SCAN discovers a queue this process never declared",
                'buildersstream_dramatiq_queue_depth{queue="smoke-probe"} 3.0' in planted,
                next(
                    (
                        ln
                        for ln in planted.splitlines()
                        if "smoke-probe" in ln and ln.startswith("buildersstream_dramatiq_queue")
                    ),
                    "no smoke-probe series",
                ),
            )
            check(
                "bookkeeping keys are not counted as queues",
                'queue="__heartbeats__"' not in planted and ".msgs" not in planted,
            )
        finally:
            probe.delete("dramatiq:smoke-probe")
        pool = [
            ln for ln in sample_lines if ln.startswith("buildersstream_db_pool_size ")
        ]
        check("pool size gauge is populated", bool(pool), pool[0] if pool else "missing")

    failed = [r for r in results if not r[1]]
    print(f"\n{'=' * 62}")
    print(f"{len(results) - len(failed)}/{len(results)} assertions passed")
    if failed:
        print("\nFAILURES:")
        for label, _, detail in failed:
            print(f"  - {label}{(' — ' + detail) if detail else ''}")
        return 1
    return 0


sys.exit(asyncio.run(main()))
