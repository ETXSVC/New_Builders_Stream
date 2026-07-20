# Estimation + E-Signature Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship product screens for cost catalogs, markup profiles, the estimate builder with PDF export and per-company branding, in-app typed e-signature for clients, and change-order management — closing every backend API gap identified in the design spec along the way.

**Architecture:** Nine backend additions (one new migration for `company_branding`, the rest are new routes on existing tables) close the gaps the spec identified. The frontend follows the Foundation/CRM+PM BFF pattern exactly: one thin Next.js Route Handler per backend call, client components using `useAuth()` + `fetch("/api/...")`, display-only state mirrors of backend rules, and the fetch-blob-anchor pattern for downloads.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async + Alembic + Dramatiq (backend, all already in place); Next.js 16 App Router + TypeScript, zero new frontend dependencies (a small hand-rolled CSV parser, no library).

---

## Spec reference

Full design: `docs/superpowers/specs/2026-07-20-estimation-esignature-frontend-design.md`. This plan implements every decision in that spec (1–13). Read it before starting if anything below is ambiguous — the spec's prose reasoning is not repeated here.

## File structure

**Backend — new files:**
- `backend/migrations/versions/0016_company_branding.py` — new table + RLS.
- `backend/app/models/company_branding.py` — `CompanyBranding` model.
- `backend/app/schemas/company_branding.py` — branding request/response schemas.
- `backend/app/routers/branding.py` — `GET`/`PUT /companies/branding`, `POST /companies/branding/logo`.
- `backend/tests/test_estimation_gaps.py` — tenant-isolation + role + 409/404 tests for every route this plan adds (PDF download, catalog/markup/estimate edit+delete, change-order GET/list, bulk import, branding).

**Backend — modified files:**
- `backend/app/models/__init__.py` — export `CompanyBranding`.
- `backend/app/routers/estimates.py` — add `GET /{id}/pdf`, `PATCH /{id}`, `DELETE /{id}`.
- `backend/app/routers/catalogs.py` — add `PATCH`/`DELETE /catalogs/items/{id}`, `POST /catalogs/items/bulk`, `PATCH`/`DELETE /markup-profiles/{id}`.
- `backend/app/routers/change_orders.py` — add `GET /change-orders/{id}`, `GET /change-orders`.
- `backend/app/schemas/estimate.py` — add `EstimatePatchRequest`; add `parent_name` to `EstimateResponse`.
- `backend/app/schemas/cost_catalog_item.py` — add `CostCatalogItemPatchRequest`, `CostCatalogItemBulkCreateRequest`, `CostCatalogItemBulkResult`, `CostCatalogItemBulkResponse`.
- `backend/app/schemas/markup_profile.py` — add `MarkupProfilePatchRequest`.
- `backend/app/schemas/change_order.py` — add `ChangeOrderResponse.project_name`.
- `backend/app/main.py` — `app.include_router(branding.router)`.

**Frontend — new files:**
- `frontend/lib/csv.ts` — parse/serialize the four-column catalog CSV format.
- `frontend/lib/api/client.ts` — (modified, see below) confirm `patch`/`delete` methods exist; add if missing.
- BFF handlers (all under `frontend/app/(app)/api/`): `estimates/route.ts`, `estimates/[id]/route.ts`, `estimates/[id]/lines/route.ts`, `estimates/[id]/calculate/route.ts`, `estimates/[id]/export/route.ts`, `estimates/[id]/pdf/route.ts`, `estimates/[id]/send-for-signature/route.ts`, `estimates/[id]/approve/route.ts`, `estimates/[id]/reject/route.ts`, `esignatures/[id]/route.ts`, `catalog/items/route.ts`, `catalog/items/bulk/route.ts`, `catalog/items/[id]/route.ts`, `catalog/items/[id]/override/route.ts`, `markup-profiles/route.ts`, `markup-profiles/[id]/route.ts`, `projects/[id]/change-orders/route.ts`, `change-orders/route.ts`, `change-orders/[id]/route.ts`, `change-orders/[id]/send-for-signature/route.ts`, `change-orders/[id]/approve/route.ts`, `change-orders/[id]/reject/route.ts`, `companies/branding/route.ts`, `companies/branding/logo/route.ts`.
- Components: `frontend/components/esign/TypedSignature.tsx`, `frontend/components/esign/SigningPanel.tsx`, `frontend/components/estimates/CatalogPanel.tsx`, `frontend/components/estimates/LineRows.tsx`, `frontend/components/estimates/EstimateBuilder.tsx`, `frontend/components/estimates/EstimateRows.tsx`, `frontend/components/estimates/NewEstimateForm.tsx`, `frontend/components/estimates/PdfPanel.tsx`, `frontend/components/change-orders/ChangeOrdersTab.tsx`, `frontend/components/catalog/CatalogItemsTab.tsx`, `frontend/components/catalog/MarkupProfilesTab.tsx`, `frontend/components/catalog/CsvImport.tsx`, `frontend/components/catalog/BrandingTab.tsx`.
- Pages: `frontend/app/(app)/estimates/page.tsx`, `frontend/app/(app)/estimates/new/page.tsx`, `frontend/app/(app)/estimates/[id]/page.tsx`, `frontend/app/(app)/catalog/page.tsx`.
- `frontend/e2e/estimation.spec.ts`.

**Frontend — modified files:**
- `frontend/components/app-shell/Nav.tsx` — Estimates + Catalog links.
- `frontend/middleware.ts` — matcher gains `/estimates/:path*`, `/catalog/:path*`.
- `frontend/app/(app)/projects/[id]/page.tsx` — add Change orders tab.
- `frontend/app/(app)/leads/[id]/page.tsx` — add an Estimates section.
- `frontend/components/projects/ClientProjectDashboard.tsx` — add the "Awaiting your signature" card.
- `frontend/lib/api/types.ts` — regenerated (Task 10).

---

## Task 1: Estimate PDF download route

**Files:**
- Modify: `backend/app/routers/estimates.py`
- Modify: `backend/tests/test_estimation_gaps.py` (new file, create in this task)

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_estimation_gaps.py`:

```python
"""Tenant-isolation, role, and error-path tests for every route this plan
adds on top of the estimation domain: PDF download, catalog/markup edit and
delete, estimate edit/delete, change-order single-GET and company-wide
list, catalog bulk import, and company branding.
"""
import io

import pytest

from tests.conftest import (
    authed_client,
    create_company_and_admin,
    set_subscription_tier,
)


@pytest.mark.asyncio
async def test_pdf_download_404_before_export(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        markup = await client.post("/markup-profiles", json={"name": "Standard"})
        project = await client.post(
            "/projects", json={"name": "Deck", "site_address": "1 Main St"}
        )
        estimate = await client.post(
            "/estimates",
            json={
                "project_id": project.json()["id"],
                "markup_profile_id": markup.json()["id"],
            },
        )
        estimate_id = estimate.json()["id"]

        response = await client.get(f"/estimates/{estimate_id}/pdf")
        assert response.status_code == 409
        assert "not ready" in response.json()["detail"].lower() or "pdf_status" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_pdf_download_streams_bytes_once_ready(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        markup = await client.post("/markup-profiles", json={"name": "Standard"})
        project = await client.post(
            "/projects", json={"name": "Deck", "site_address": "1 Main St"}
        )
        estimate = await client.post(
            "/estimates",
            json={
                "project_id": project.json()["id"],
                "markup_profile_id": markup.json()["id"],
            },
        )
        estimate_id = estimate.json()["id"]

        export_response = await client.post(f"/estimates/{estimate_id}/export")
        assert export_response.status_code == 202

        # generate_estimate_pdf is a Dramatiq actor enqueued via .send(); run
        # its plain-coroutine implementation directly, same pattern
        # test_pdf_export.py already established for this exact actor.
        from app.tasks.estimate_pdf import _generate_estimate_pdf

        await _generate_estimate_pdf(estimate_id, str(_admin_user_id(admin_token)))

        response = await client.get(f"/estimates/{estimate_id}/pdf")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_pdf_download_cross_tenant_404(async_client):
    company_a, admin_a = await create_company_and_admin(async_client)
    company_b, admin_b = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_a, company_a) as client_a:
        markup = await client_a.post("/markup-profiles", json={"name": "Standard"})
        project = await client_a.post(
            "/projects", json={"name": "Deck", "site_address": "1 Main St"}
        )
        estimate = await client_a.post(
            "/estimates",
            json={
                "project_id": project.json()["id"],
                "markup_profile_id": markup.json()["id"],
            },
        )
        estimate_id = estimate.json()["id"]

    async with authed_client(async_client, admin_b, company_b) as client_b:
        response = await client_b.get(f"/estimates/{estimate_id}/pdf")
        assert response.status_code == 404
```

`_admin_user_id` doesn't exist yet — check `tests/conftest.py` for whatever helper already extracts a user id from a JWT/token fixture (search for `decode` or `user_id` in that file) and use it instead of inventing a new one; if none exists, replace that call with `admin_id` returned directly from `create_company_and_admin` (check its actual return signature in `tests/conftest.py` before writing this test — some conftest helpers return `(company_id, token)`, others `(company_id, token, user_id)`; match whichever this codebase's version returns).

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_estimation_gaps.py -v`
Expected: FAIL — `404 Not Found` (route doesn't exist) or similar, not the asserted codes.

- [ ] **Step 3: Implement `GET /estimates/{id}/pdf`**

In `backend/app/routers/estimates.py`, add imports at the top:

```python
from fastapi.responses import Response

from app.config import settings
```

Add this route after `export_estimate_pdf`:

```python
@router.get("/{estimate_id}/pdf")
async def download_estimate_pdf(
    estimate_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> Response:
    """Streams the exported PDF from `pdf_storage_path`. Same read roles as
    `get_estimate` (admin/PM/accountant/client) — a client needs this to
    actually see what they're about to sign, same reasoning `_READ_ROLES`
    already documents at the top of this module.

    409, not 404, when `pdf_status != "ready"`: the Estimate itself exists
    and is visible, it just has no artifact to serve yet (or export failed) —
    a real, reachable state, not "doesn't exist."
    """
    estimate = await _get_estimate_or_404(current, estimate_id)

    if estimate.pdf_status != "ready" or estimate.pdf_storage_path is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Estimate PDF is not ready (pdf_status={estimate.pdf_status!r})",
        )

    absolute_path = Path(settings.storage_root) / estimate.pdf_storage_path
    pdf_bytes = absolute_path.read_bytes()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="estimate-{estimate.id}.pdf"'},
    )
```

Add `from pathlib import Path` to the existing import block if not already present (it isn't — check the current imports at the top of the file before adding, to avoid a duplicate).

- [ ] **Step 4: Run to verify it passes**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_estimation_gaps.py -v`
Expected: the three tests in this task's Step 1 PASS. (Tests for later steps of this task file will still fail — that's expected until those steps are implemented; run with `-k pdf` to isolate this task's tests if the file already has more added by then.)

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/estimates.py backend/tests/test_estimation_gaps.py
git commit -m "feat: GET /estimates/{id}/pdf download route"
```

## Task 2: Cost catalog item edit + delete

**Files:**
- Modify: `backend/app/schemas/cost_catalog_item.py`
- Modify: `backend/app/routers/catalogs.py`
- Modify: `backend/tests/test_estimation_gaps.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_estimation_gaps.py`:

```python
@pytest.mark.asyncio
async def test_patch_catalog_item_updates_rate(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        item = await client.post(
            "/catalogs/items",
            json={"category": "Framing", "name": "Lumber", "unit": "bf", "unit_rate": "4.00"},
        )
        item_id = item.json()["id"]

        response = await client.patch(f"/catalogs/items/{item_id}", json={"unit_rate": "4.50"})
        assert response.status_code == 200
        assert response.json()["unit_rate"] == "4.50"
        assert response.json()["name"] == "Lumber"  # untouched field preserved


@pytest.mark.asyncio
async def test_patch_catalog_item_cross_tenant_404(async_client):
    company_a, admin_a = await create_company_and_admin(async_client)
    company_b, admin_b = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_a, company_a) as client_a:
        item = await client_a.post(
            "/catalogs/items",
            json={"category": "Framing", "name": "Lumber", "unit": "bf", "unit_rate": "4.00"},
        )
        item_id = item.json()["id"]

    async with authed_client(async_client, admin_b, company_b) as client_b:
        response = await client_b.patch(f"/catalogs/items/{item_id}", json={"unit_rate": "9.00"})
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_catalog_item_blocked_when_referenced(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        item = await client.post(
            "/catalogs/items",
            json={"category": "Framing", "name": "Lumber", "unit": "bf", "unit_rate": "4.00"},
        )
        item_id = item.json()["id"]
        markup = await client.post("/markup-profiles", json={"name": "Standard"})
        project = await client.post(
            "/projects", json={"name": "Deck", "site_address": "1 Main St"}
        )
        estimate = await client.post(
            "/estimates",
            json={"project_id": project.json()["id"], "markup_profile_id": markup.json()["id"]},
        )
        await client.put(
            f"/estimates/{estimate.json()['id']}/lines",
            json={"items": [{"cost_catalog_item_id": item_id, "quantity": "10"}]},
        )

        response = await client.delete(f"/catalogs/items/{item_id}")
        assert response.status_code == 409


@pytest.mark.asyncio
async def test_delete_catalog_item_blocked_when_overridden(async_client):
    # Parent company creates an item; a child branch overrides it; deleting
    # the parent's original must 409, not silently orphan the override (the
    # model's ondelete="SET NULL" would otherwise let this succeed and turn
    # the override into a standalone item without warning).
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        item = await client.post(
            "/catalogs/items",
            json={"category": "Framing", "name": "Lumber", "unit": "bf", "unit_rate": "4.00"},
        )
        item_id = item.json()["id"]
        child = await client.post(
            "/companies", json={"name": "Child Co", "parent_company_id": company_id}
        )

    # Switch tenant context to the child to create the override — match
    # whatever pattern test_cost_catalog_inheritance.py already uses for a
    # child-branch-scoped request (X-Tenant-ID header via authed_client, or
    # a dedicated child-session fixture); read that test file's setup before
    # writing this block if authed_client's signature doesn't already accept
    # a company override.
    async with authed_client(async_client, admin_token, child.json()["id"]) as child_client:
        await child_client.post(f"/catalogs/items/{item_id}/override", json={
            "category": "Framing", "name": "Better Lumber", "unit": "bf", "unit_rate": "5.00",
        })

    async with authed_client(async_client, admin_token, company_id) as client:
        response = await client.delete(f"/catalogs/items/{item_id}")
        assert response.status_code == 409


@pytest.mark.asyncio
async def test_delete_catalog_item_succeeds_when_unreferenced(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        item = await client.post(
            "/catalogs/items",
            json={"category": "Framing", "name": "Lumber", "unit": "bf", "unit_rate": "4.00"},
        )
        item_id = item.json()["id"]

        response = await client.delete(f"/catalogs/items/{item_id}")
        assert response.status_code == 204

        get_response = await client.get("/catalogs/items")
        assert item_id not in [i["id"] for i in get_response.json()["items"]]
```

Before writing these, open `backend/tests/test_cost_catalog_inheritance.py` and copy its exact pattern for creating a child company and switching tenant context — do not guess at `authed_client`'s signature or a company-creation route's exact body shape; match what that file actually does.

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_estimation_gaps.py -k catalog_item -v`
Expected: FAIL (routes don't exist yet).

- [ ] **Step 3: Add the schemas**

In `backend/app/schemas/cost_catalog_item.py`, add after `CostCatalogItemCreateRequest`:

```python
class CostCatalogItemPatchRequest(BaseModel):
    """Body for `PATCH /catalogs/items/{id}`. All fields optional — a PATCH
    only touches what's supplied, matching `ProjectPatchRequest`'s own
    all-optional convention (`app/schemas/project.py`)."""

    category: str | None = Field(None, min_length=1, max_length=100)
    name: str | None = Field(None, min_length=1, max_length=255)
    unit: str | None = Field(None, min_length=1, max_length=50)
    unit_rate: Decimal | None = None
```

- [ ] **Step 4: Implement the routes**

In `backend/app/routers/catalogs.py`, add imports:

```python
from app.models import Estimate, EstimateLineItem
from app.schemas.cost_catalog_item import CostCatalogItemPatchRequest
```

(Merge into the existing `from app.schemas.cost_catalog_item import (...)` block rather than a second import line for that module.)

Add a shared lookup helper and the two routes, placed after `create_catalog_item_override` and before `_paginate_resolved_items`:

```python
async def _get_catalog_item_or_404(current: CurrentUser, item_id: uuid.UUID) -> CostCatalogItem:
    result = await current.session.execute(
        select(CostCatalogItem).where(CostCatalogItem.id == item_id)
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Catalog item not found")
    return item


@router.patch("/catalogs/items/{item_id}", response_model=CostCatalogItemResponse)
async def update_catalog_item(
    item_id: uuid.UUID,
    payload: CostCatalogItemPatchRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> CostCatalogItemResponse:
    """Edits category/name/unit/unit_rate on an existing item. Does not
    touch `EstimateLineItem.unit_rate_snapshot` on any estimate that already
    referenced this item at some past rate — snapshots are immutable by
    design (see `replace_estimate_line_items`'s own docstring in
    estimates.py); only future line-item adds/recalculates see the new
    rate."""
    item = await _get_catalog_item_or_404(current, item_id)

    if payload.category is not None:
        item.category = payload.category
    if payload.name is not None:
        item.name = payload.name
    if payload.unit is not None:
        item.unit = payload.unit
    if payload.unit_rate is not None:
        item.unit_rate = payload.unit_rate

    await current.session.flush()
    return CostCatalogItemResponse.model_validate(item)


@router.delete("/catalogs/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_catalog_item(
    item_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> None:
    """409 if any EstimateLineItem references this item, or any other
    CostCatalogItem overrides it (parent_catalog_item_id points here) —
    both are real, in-use references the model's own ondelete behavior
    (SET NULL for overrides; no FK at all constrains line items, since
    unit_rate_snapshot already copied the rate) would otherwise let this
    delete silently proceed through, orphaning history a caller almost
    certainly didn't intend to lose. Checked before the DELETE is issued —
    one transaction, one outcome, same discipline every other guarded
    mutation in this codebase uses.
    """
    item = await _get_catalog_item_or_404(current, item_id)

    line_item_result = await current.session.execute(
        select(EstimateLineItem.id).where(EstimateLineItem.cost_catalog_item_id == item_id).limit(1)
    )
    if line_item_result.scalar_one_or_none() is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Cannot delete a catalog item referenced by an estimate line item",
        )

    override_result = await current.session.execute(
        select(CostCatalogItem.id).where(CostCatalogItem.parent_catalog_item_id == item_id).limit(1)
    )
    if override_result.scalar_one_or_none() is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Cannot delete a catalog item that has child-company overrides",
        )

    await current.session.delete(item)
    await current.session.flush()
```

- [ ] **Step 5: Run to verify it passes**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_estimation_gaps.py -k catalog_item -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/cost_catalog_item.py backend/app/routers/catalogs.py backend/tests/test_estimation_gaps.py
git commit -m "feat: catalog item edit and delete routes"
```

## Task 3: Markup profile edit + delete

**Files:**
- Modify: `backend/app/schemas/markup_profile.py`
- Modify: `backend/app/routers/catalogs.py`
- Modify: `backend/tests/test_estimation_gaps.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_estimation_gaps.py`:

```python
@pytest.mark.asyncio
async def test_patch_markup_profile(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        profile = await client.post(
            "/markup-profiles", json={"name": "Standard", "overhead_pct": "10", "profit_pct": "15"}
        )
        profile_id = profile.json()["id"]

        response = await client.patch(f"/markup-profiles/{profile_id}", json={"profit_pct": "20"})
        assert response.status_code == 200
        assert response.json()["profit_pct"] == "20.00"
        assert response.json()["overhead_pct"] == "10.00"


@pytest.mark.asyncio
async def test_delete_markup_profile_blocked_when_referenced(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        profile = await client.post("/markup-profiles", json={"name": "Standard"})
        project = await client.post(
            "/projects", json={"name": "Deck", "site_address": "1 Main St"}
        )
        await client.post(
            "/estimates",
            json={"project_id": project.json()["id"], "markup_profile_id": profile.json()["id"]},
        )

        response = await client.delete(f"/markup-profiles/{profile.json()['id']}")
        assert response.status_code == 409


@pytest.mark.asyncio
async def test_delete_markup_profile_succeeds_when_unreferenced(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        profile = await client.post("/markup-profiles", json={"name": "Standard"})
        response = await client.delete(f"/markup-profiles/{profile.json()['id']}")
        assert response.status_code == 204
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_estimation_gaps.py -k markup_profile -v`
Expected: FAIL.

- [ ] **Step 3: Add the schema**

In `backend/app/schemas/markup_profile.py`, add after `MarkupProfileCreateRequest`:

```python
class MarkupProfilePatchRequest(BaseModel):
    """Body for `PATCH /markup-profiles/{id}`. All fields optional, same
    partial-update convention as `CostCatalogItemPatchRequest`."""

    name: str | None = Field(None, min_length=1, max_length=255)
    overhead_pct: Decimal | None = None
    profit_pct: Decimal | None = None
```

Add `Field` to the existing `from pydantic import BaseModel, ConfigDict` import line (becomes `from pydantic import BaseModel, ConfigDict, Field`).

- [ ] **Step 4: Implement the routes**

In `backend/app/routers/catalogs.py`, add to the existing `from app.schemas.markup_profile import (...)` block: `MarkupProfilePatchRequest`. Add `Estimate` to the `from app.models import CostCatalogItem, MarkupProfile` line (becomes `from app.models import CostCatalogItem, Estimate, EstimateLineItem, MarkupProfile` — merge with Task 2's `Estimate`/`EstimateLineItem` addition into one import line).

Add after `create_markup_profile`:

```python
async def _get_markup_profile_or_404(current: CurrentUser, profile_id: uuid.UUID) -> MarkupProfile:
    result = await current.session.execute(
        select(MarkupProfile).where(MarkupProfile.id == profile_id)
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Markup profile not found")
    return profile


@router.patch("/markup-profiles/{profile_id}", response_model=MarkupProfileResponse)
async def update_markup_profile(
    profile_id: uuid.UUID,
    payload: MarkupProfilePatchRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> MarkupProfileResponse:
    profile = await _get_markup_profile_or_404(current, profile_id)

    if payload.name is not None:
        profile.name = payload.name
    if payload.overhead_pct is not None:
        profile.overhead_pct = payload.overhead_pct
    if payload.profit_pct is not None:
        profile.profit_pct = payload.profit_pct

    await current.session.flush()
    return MarkupProfileResponse.model_validate(profile)


@router.delete("/markup-profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_markup_profile(
    profile_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> None:
    """409 if any Estimate references this profile — same "real, in-use
    reference blocks the delete" reasoning as delete_catalog_item above."""
    profile = await _get_markup_profile_or_404(current, profile_id)

    estimate_result = await current.session.execute(
        select(Estimate.id).where(Estimate.markup_profile_id == profile_id).limit(1)
    )
    if estimate_result.scalar_one_or_none() is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Cannot delete a markup profile referenced by an estimate",
        )

    await current.session.delete(profile)
    await current.session.flush()
```

- [ ] **Step 5: Run to verify it passes**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_estimation_gaps.py -k markup_profile -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/markup_profile.py backend/app/routers/catalogs.py backend/tests/test_estimation_gaps.py
git commit -m "feat: markup profile edit and delete routes"
```

## Task 4: Estimate edit + delete (draft-only)

**Files:**
- Modify: `backend/app/schemas/estimate.py`
- Modify: `backend/app/routers/estimates.py`
- Modify: `backend/tests/test_estimation_gaps.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_estimation_gaps.py`:

```python
@pytest.mark.asyncio
async def test_patch_estimate_changes_markup_profile_while_draft(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        markup_a = await client.post("/markup-profiles", json={"name": "A"})
        markup_b = await client.post("/markup-profiles", json={"name": "B"})
        project = await client.post(
            "/projects", json={"name": "Deck", "site_address": "1 Main St"}
        )
        estimate = await client.post(
            "/estimates",
            json={"project_id": project.json()["id"], "markup_profile_id": markup_a.json()["id"]},
        )

        response = await client.patch(
            f"/estimates/{estimate.json()['id']}",
            json={"markup_profile_id": markup_b.json()["id"]},
        )
        assert response.status_code == 200
        assert response.json()["markup_profile_id"] == markup_b.json()["id"]


@pytest.mark.asyncio
async def test_patch_estimate_409_once_sent(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        markup = await client.post("/markup-profiles", json={"name": "A"})
        project = await client.post(
            "/projects", json={"name": "Deck", "site_address": "1 Main St"}
        )
        estimate = await client.post(
            "/estimates",
            json={"project_id": project.json()["id"], "markup_profile_id": markup.json()["id"]},
        )
        estimate_id = estimate.json()["id"]
        await client.put(f"/estimates/{estimate_id}/lines", json={"items": []})
        await client.post(f"/estimates/{estimate_id}/calculate")
        await client.post(f"/estimates/{estimate_id}/send-for-signature")

        response = await client.patch(
            f"/estimates/{estimate_id}", json={"markup_profile_id": markup.json()["id"]}
        )
        assert response.status_code == 409


@pytest.mark.asyncio
async def test_delete_estimate_while_draft(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        markup = await client.post("/markup-profiles", json={"name": "A"})
        project = await client.post(
            "/projects", json={"name": "Deck", "site_address": "1 Main St"}
        )
        estimate = await client.post(
            "/estimates",
            json={"project_id": project.json()["id"], "markup_profile_id": markup.json()["id"]},
        )

        response = await client.delete(f"/estimates/{estimate.json()['id']}")
        assert response.status_code == 204

        get_response = await client.get(f"/estimates/{estimate.json()['id']}")
        assert get_response.status_code == 404


@pytest.mark.asyncio
async def test_delete_estimate_409_once_sent(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        markup = await client.post("/markup-profiles", json={"name": "A"})
        project = await client.post(
            "/projects", json={"name": "Deck", "site_address": "1 Main St"}
        )
        estimate = await client.post(
            "/estimates",
            json={"project_id": project.json()["id"], "markup_profile_id": markup.json()["id"]},
        )
        estimate_id = estimate.json()["id"]
        await client.put(f"/estimates/{estimate_id}/lines", json={"items": []})
        await client.post(f"/estimates/{estimate_id}/calculate")
        await client.post(f"/estimates/{estimate_id}/send-for-signature")

        response = await client.delete(f"/estimates/{estimate_id}")
        assert response.status_code == 409
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_estimation_gaps.py -k "patch_estimate or delete_estimate" -v`
Expected: FAIL.

- [ ] **Step 3: Add the schema**

In `backend/app/schemas/estimate.py`, add after `EstimateCreateRequest`:

```python
class EstimatePatchRequest(BaseModel):
    """Body for `PATCH /estimates/{id}` — draft-only. Only `markup_profile_id`
    is editable (spec Decision 1, item 5): the project/lead binding is
    immutable after creation, matching `EstimateCreateRequest`'s own
    "exactly one of project_id/lead_id, decided once at creation" framing."""

    markup_profile_id: uuid.UUID
```

- [ ] **Step 4: Implement the routes**

In `backend/app/routers/estimates.py`, add `EstimatePatchRequest` to the existing `from app.schemas.estimate import (...)` block.

Add after `create_estimate`:

```python
@router.patch("/{estimate_id}", response_model=EstimateResponse)
async def update_estimate(
    estimate_id: uuid.UUID,
    payload: EstimatePatchRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> EstimateResponse:
    """Draft-only (spec Decision 1, item 5) — 409 once sent/approved/rejected,
    same "existence/tenant before semantic validation" ordering every other
    guarded mutation in this router uses. Only `markup_profile_id` is
    accepted; changing it does NOT retroactively touch already-computed
    `subtotal`/`total` or any line item's `unit_rate_snapshot` — a caller
    must re-run `POST /calculate` to see the new markup applied, same
    "recalculation is a deliberate, explicit step" precedent
    `calculate_estimate_totals`'s own docstring establishes.
    """
    estimate = await _get_estimate_or_404(current, estimate_id)

    if estimate.status != "draft":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Estimate must be in 'draft' status to edit, got '{estimate.status}'",
        )

    markup_result = await current.session.execute(
        select(MarkupProfile).where(MarkupProfile.id == payload.markup_profile_id)
    )
    if markup_result.scalar_one_or_none() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Markup profile not found")

    estimate.markup_profile_id = payload.markup_profile_id
    await current.session.flush()
    return EstimateResponse.model_validate(estimate)


@router.delete("/{estimate_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_estimate(
    estimate_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> None:
    """Draft-only, same guard as update_estimate above. Line items cascade
    with the parent row at the DB level (check migration 0007's FK — if it
    lacks ON DELETE CASCADE on estimate_line_items.estimate_id, delete line
    items explicitly here first via
    `delete(EstimateLineItem).where(EstimateLineItem.estimate_id == estimate.id)`
    before deleting the estimate itself, to avoid an FK violation)."""
    estimate = await _get_estimate_or_404(current, estimate_id)

    if estimate.status != "draft":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Estimate must be in 'draft' status to delete, got '{estimate.status}'",
        )

    await current.session.delete(estimate)
    await current.session.flush()
```

Before finalizing this step, check `backend/migrations/versions/0007_*.py` (find the exact filename — search `migrations/versions/` for the estimates-table migration) for whether `estimate_line_items.estimate_id`'s FK has `ondelete="CASCADE"`. If it does not, add the explicit `delete(EstimateLineItem)...` line (with `from sqlalchemy import delete` already imported at the top of this file) before `current.session.delete(estimate)` in the function above.

- [ ] **Step 5: Run to verify it passes**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_estimation_gaps.py -k "patch_estimate or delete_estimate" -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/estimate.py backend/app/routers/estimates.py backend/tests/test_estimation_gaps.py
git commit -m "feat: estimate edit and delete routes (draft-only)"
```

## Task 5: Single change-order GET + company-wide list

**Files:**
- Modify: `backend/app/schemas/change_order.py`
- Modify: `backend/app/routers/change_orders.py`
- Modify: `backend/tests/test_estimation_gaps.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_estimation_gaps.py`:

```python
@pytest.mark.asyncio
async def test_get_single_change_order(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        project = await client.post(
            "/projects", json={"name": "Deck", "site_address": "1 Main St"}
        )
        await client.patch(
            f"/projects/{project.json()['id']}/status", json={"status": "pre_construction"}
        )
        await client.patch(
            f"/projects/{project.json()['id']}/status", json={"status": "active"}
        )
        co = await client.post(
            f"/projects/{project.json()['id']}/change-orders",
            json={"description": "Add deck stairs", "cost_delta": "500.00"},
        )

        response = await client.get(f"/change-orders/{co.json()['id']}")
        assert response.status_code == 200
        assert response.json()["description"] == "Add deck stairs"


@pytest.mark.asyncio
async def test_get_single_change_order_cross_tenant_404(async_client):
    company_a, admin_a = await create_company_and_admin(async_client)
    company_b, admin_b = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_a, company_a) as client_a:
        project = await client_a.post(
            "/projects", json={"name": "Deck", "site_address": "1 Main St"}
        )
        await client_a.patch(
            f"/projects/{project.json()['id']}/status", json={"status": "pre_construction"}
        )
        await client_a.patch(
            f"/projects/{project.json()['id']}/status", json={"status": "active"}
        )
        co = await client_a.post(
            f"/projects/{project.json()['id']}/change-orders",
            json={"description": "Add deck stairs", "cost_delta": "500.00"},
        )

    async with authed_client(async_client, admin_b, company_b) as client_b:
        response = await client_b.get(f"/change-orders/{co.json()['id']}")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_all_change_orders_scoped_to_pending_for_client(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        project = await client.post(
            "/projects", json={"name": "Deck", "site_address": "1 Main St"}
        )
        await client.patch(
            f"/projects/{project.json()['id']}/status", json={"status": "pre_construction"}
        )
        await client.patch(
            f"/projects/{project.json()['id']}/status", json={"status": "active"}
        )
        await client.post(
            f"/projects/{project.json()['id']}/change-orders",
            json={"description": "Pending one", "cost_delta": "500.00"},
        )

        response = await client.get("/change-orders")
        assert response.status_code == 200
        assert len(response.json()["items"]) == 1
        assert response.json()["items"][0]["project_name"] == "Deck"
```

Adapt the project-status-transition calls above to match this codebase's actual `PATCH /projects/{id}/status` body shape (`{"status": "..."}` vs `{"new_status": "..."}` — check `test_project_status_transitions.py` or `app/schemas/project.py`'s `ProjectStatusUpdateRequest` before finalizing; the field name shown here is a placeholder to verify, not to blindly copy).

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_estimation_gaps.py -k change_order -v`
Expected: FAIL for the new tests (existing project-status setup lines should already pass if the body shape is correct — fix that first if those lines themselves 422).

- [ ] **Step 3: Add `project_name` to the response schema**

In `backend/app/schemas/change_order.py`, add to `ChangeOrderResponse`:

```python
class ChangeOrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    company_id: uuid.UUID
    description: str
    cost_delta: Decimal
    schedule_impact_days: int
    status: str
    esignature_id: uuid.UUID | None
    created_at: datetime
    # Populated by the router via a join (not a mapped relationship on
    # ChangeOrder) — see list_all_change_orders below. `create_change_order`/
    # `list_change_orders` (nested-under-project routes, where the caller
    # already knows the project) still pass this through
    # model_validate(change_order) with project_name simply absent from the
    # instance; Pydantic v2's from_attributes leaves an unset field at its
    # default. Given a default of None here (not a required field) so those
    # two existing call sites don't need to change.
    project_name: str | None = None
```

Add `project_name: str | None = None` as shown; existing call sites (`create_change_order`, `list_change_orders`, `send_change_order_for_signature`, `approve_change_order`, `reject_change_order` — all still calling `ChangeOrderResponse.model_validate(change_order)` directly) need no changes since the field defaults to `None`.

- [ ] **Step 4: Implement the routes**

In `backend/app/routers/change_orders.py`, add `Project` to the `from app.models import ChangeOrder` line (becomes `from app.models import ChangeOrder, Project`).

Add after `_get_change_order_or_404`:

```python
@router.get("/change-orders/{change_order_id}", response_model=ChangeOrderResponse)
async def get_change_order(
    change_order_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> ChangeOrderResponse:
    """No client status scoping here (unlike list_change_orders/
    list_all_change_orders below) — same "direct-by-id access isn't scoped,
    only list-and-act-on-it flows are" precedent `_get_estimate_or_404`'s
    docstring establishes for Estimates."""
    change_order = await _get_change_order_or_404(current, change_order_id)
    return ChangeOrderResponse.model_validate(change_order)


@router.get("/change-orders", response_model=ChangeOrderListResponse)
async def list_all_change_orders(
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> ChangeOrderListResponse:
    """Company-wide (not nested under a project_id) — the discovery
    mechanism a client needs to find every pending Change Order awaiting
    their action across ALL their projects, without N per-project list
    calls (spec Decision 1, item 8). `client` is scoped to `status="pending"`,
    same as list_change_orders' own per-project scoping.

    Joined to `projects` for `project_name` — a bare ChangeOrder row alone
    isn't enough context for a cross-project list row (unlike the
    per-project list, where the caller already knows which project they're
    looking at).
    """
    if status_filter is not None and status_filter not in ("pending", "approved", "rejected"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "status must be one of ('pending', 'approved', 'rejected')",
        )

    query = select(ChangeOrder, Project.name).join(Project, ChangeOrder.project_id == Project.id)

    if current.role == "client":
        query = query.where(ChangeOrder.status == "pending")
    if status_filter is not None:
        query = query.where(ChangeOrder.status == status_filter)

    query = query.order_by(ChangeOrder.created_at.asc(), ChangeOrder.id.asc()).limit(limit + 1)
    if cursor is not None:
        from app.core.pagination import decode_cursor

        cursor_created_at, cursor_id = decode_cursor(cursor)
        query = query.where(
            (ChangeOrder.created_at, ChangeOrder.id) > (cursor_created_at, cursor_id)
        )

    result = await current.session.execute(query)
    rows = result.all()

    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        last_co, _ = rows[-1]
        from app.core.pagination import encode_cursor

        next_cursor = encode_cursor(last_co.created_at, last_co.id)

    items = []
    for change_order, project_name in rows:
        response = ChangeOrderResponse.model_validate(change_order)
        response.project_name = project_name
        items.append(response)

    return ChangeOrderListResponse(items=items, next_cursor=next_cursor)
```

This route hand-rolls cursor pagination (join queries can't pass a plain `Select[tuple[Row]]` through `paginate()` the way a single-model query can) rather than reusing `paginate()` directly — mirroring `catalogs.py`'s own precedent of a bespoke pagination helper where the generic one doesn't fit. Move the `decode_cursor`/`encode_cursor` imports to the top-level import block (`from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor, paginate`) rather than leaving them as inline imports inside the function — the inline placement above is only to make the diff obvious in this plan step; the actual code should have clean top-of-file imports like every other router in this codebase.

- [ ] **Step 5: Run to verify it passes**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_estimation_gaps.py -k change_order -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/change_order.py backend/app/routers/change_orders.py backend/tests/test_estimation_gaps.py
git commit -m "feat: single change-order GET and company-wide list"
```

## Task 6: `parent_name` on estimate list rows

**Files:**
- Modify: `backend/app/schemas/estimate.py`
- Modify: `backend/app/routers/estimates.py`
- Modify: `backend/tests/test_estimation_gaps.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_estimation_gaps.py`:

```python
@pytest.mark.asyncio
async def test_estimate_list_includes_parent_name_for_project_and_lead(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        markup = await client.post("/markup-profiles", json={"name": "Standard"})
        project = await client.post(
            "/projects", json={"name": "Kitchen Remodel", "site_address": "1 Main St"}
        )
        await client.post(
            "/estimates",
            json={"project_id": project.json()["id"], "markup_profile_id": markup.json()["id"]},
        )

        lead = await client.post(
            "/leads",
            json={
                "contact_name": "Ada",
                "project_name": "Bathroom Remodel",
                "email": "ada@example.com",
                "project_type": "Remodel",
            },
        )
        for _ in range(2):
            await client.patch(f"/leads/{lead.json()['id']}", json={"status": "contacted"})
        # Advance far enough for _LEAD_STATUSES_ELIGIBLE_FOR_ESTIMATE — check
        # the exact transition call shape used elsewhere in this test suite
        # (test_lead_status_transitions.py) rather than guessing PATCH vs a
        # dedicated transition route; adjust these lines to match.

        response = await client.get("/estimates")
        assert response.status_code == 200
        names = {item["parent_name"] for item in response.json()["items"]}
        assert "Kitchen Remodel" in names
```

Simplify the lead-transition portion to whatever this codebase's actual lead-transition test helper is (search `test_lead_status_transitions.py` or `test_crm_tenant_isolation.py` for the pattern) — the exact mechanics aren't this task's focus, only that a lead-backed estimate's `parent_name` resolves to the lead's `project_name` field. If getting a lead to an estimate-eligible status is awkward to set up, it's acceptable to only assert the project-backed case and drop the lead half of this test — the router implementation below still needs to handle both.

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_estimation_gaps.py -k parent_name -v`
Expected: FAIL — `parent_name` absent from the response.

- [ ] **Step 3: Add the field**

In `backend/app/schemas/estimate.py`, add to `EstimateResponse`:

```python
    created_at: datetime
    updated_at: datetime
    # Populated by the router via a join to Project.name or Lead.project_name
    # — not a mapped relationship on Estimate. Defaults to None so
    # create_estimate's existing `EstimateResponse.model_validate(estimate)`
    # call (no join available there) doesn't need to change.
    parent_name: str | None = None
```

- [ ] **Step 4: Implement in the router**

In `backend/app/routers/estimates.py`, modify `list_estimates`:

```python
@router.get("", response_model=EstimateListResponse)
async def list_estimates(
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> EstimateListResponse:
    query = select(Estimate)

    if current.role == "client":
        query = query.where(Estimate.status == "sent")

    if status_filter is not None:
        query = query.where(Estimate.status == status_filter)

    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=Estimate.created_at,
        id_col=Estimate.id,
        cursor=cursor,
        limit=limit,
    )

    # parent_name resolution: two disjoint id sets (project-backed vs
    # lead-backed estimates in this page), each resolved with one query —
    # avoids N+1 without needing a join in the base paginate() query (which
    # would have to LEFT JOIN both projects and leads and coalesce, adding
    # complexity to the one Select paginate() operates on for a page that
    # may be entirely one kind or the other).
    project_ids = {row.project_id for row in rows if row.project_id is not None}
    lead_ids = {row.lead_id for row in rows if row.lead_id is not None}

    project_names: dict[uuid.UUID, str] = {}
    if project_ids:
        project_result = await current.session.execute(
            select(Project.id, Project.name).where(Project.id.in_(project_ids))
        )
        project_names = dict(project_result.all())

    lead_names: dict[uuid.UUID, str] = {}
    if lead_ids:
        lead_result = await current.session.execute(
            select(Lead.id, Lead.project_name).where(Lead.id.in_(lead_ids))
        )
        lead_names = dict(lead_result.all())

    items = []
    for row in rows:
        response = EstimateResponse.model_validate(row)
        if row.project_id is not None:
            response.parent_name = project_names.get(row.project_id)
        elif row.lead_id is not None:
            response.parent_name = lead_names.get(row.lead_id)
        items.append(response)

    return EstimateListResponse(items=items, next_cursor=next_cursor)
```

`Project` and `Lead` are already imported at the top of `estimates.py` (`from app.models import CostCatalogItem, Estimate, EstimateLineItem, Lead, MarkupProfile, Project`) — no import changes needed for this step.

- [ ] **Step 5: Run to verify it passes**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_estimation_gaps.py -k parent_name -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/estimate.py backend/app/routers/estimates.py backend/tests/test_estimation_gaps.py
git commit -m "feat: parent_name enrichment on estimate list rows"
```

## Task 7: Catalog item bulk import

**Files:**
- Modify: `backend/app/schemas/cost_catalog_item.py`
- Modify: `backend/app/routers/catalogs.py`
- Modify: `backend/tests/test_estimation_gaps.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_estimation_gaps.py`:

```python
@pytest.mark.asyncio
async def test_bulk_import_partial_failure_reports_per_row(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        response = await client.post(
            "/catalogs/items/bulk",
            json={
                "items": [
                    {"category": "Framing", "name": "Lumber", "unit": "bf", "unit_rate": "4.00"},
                    {"category": "Framing", "name": "", "unit": "bf", "unit_rate": "4.00"},
                ]
            },
        )
        assert response.status_code == 200
        results = response.json()["results"]
        assert results[0]["status"] == "created"
        assert results[1]["status"] == "error"

        list_response = await client.get("/catalogs/items")
        assert len(list_response.json()["items"]) == 1


@pytest.mark.asyncio
async def test_bulk_import_rejects_over_500_rows(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        items = [
            {"category": "C", "name": f"Item {i}", "unit": "ea", "unit_rate": "1.00"}
            for i in range(501)
        ]
        response = await client.post("/catalogs/items/bulk", json={"items": items})
        assert response.status_code == 422
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_estimation_gaps.py -k bulk_import -v`
Expected: FAIL.

- [ ] **Step 3: Add the schemas**

In `backend/app/schemas/cost_catalog_item.py`, add at the end of the file:

```python
class CostCatalogItemBulkCreateRequest(BaseModel):
    """Body for `POST /catalogs/items/bulk` — CSV import. Max 500 rows per
    call (spec Decision 9): large enough for a real catalog seed, small
    enough that one request stays well within normal timeout/payload
    budgets without needing chunked upload."""

    items: list[CostCatalogItemCreateRequest] = Field(..., max_length=500)


class CostCatalogItemBulkResultEntry(BaseModel):
    """One row's outcome. `detail` is populated on `status="error"` only —
    the created item's id isn't returned per-row (the caller can re-list to
    see the full resulting catalog); this response is a validation report,
    not a bulk-create response envelope."""

    index: int
    status: str
    detail: str | None = None


class CostCatalogItemBulkResponse(BaseModel):
    results: list[CostCatalogItemBulkResultEntry]
```

- [ ] **Step 4: Implement the route**

In `backend/app/routers/catalogs.py`, add to the `from app.schemas.cost_catalog_item import (...)` block: `CostCatalogItemBulkCreateRequest`, `CostCatalogItemBulkResponse`, `CostCatalogItemBulkResultEntry`.

Add after `create_catalog_item_override`, before `_paginate_resolved_items`:

```python
@router.post("/catalogs/items/bulk", response_model=CostCatalogItemBulkResponse)
async def bulk_create_catalog_items(
    payload: CostCatalogItemBulkCreateRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> CostCatalogItemBulkResponse:
    """CSV-import batch create. Each row is inserted independently — one
    bad row (already caught at the Pydantic layer for most malformed
    input, since `items` is `list[CostCatalogItemCreateRequest]`) does not
    abort the rest. `Field(..., max_length=500)` on the request schema
    already rejects an oversized batch with a 422 before this handler body
    runs at all, so no additional length check is needed here.

    Every successfully created row is flushed individually (not batched
    into one flush at the end) so a later row's insert failure — e.g. a
    DB-level constraint this schema doesn't already catch — doesn't roll
    back an earlier row's success within the same request; each row is its
    own outcome, matching the per-row report this route promises.
    """
    results: list[CostCatalogItemBulkResultEntry] = []

    for index, item_payload in enumerate(payload.items):
        try:
            item = CostCatalogItem(
                company_id=current.company_id,
                parent_catalog_item_id=None,
                category=item_payload.category,
                name=item_payload.name,
                unit=item_payload.unit,
                unit_rate=item_payload.unit_rate,
            )
            current.session.add(item)
            await current.session.flush()
            results.append(CostCatalogItemBulkResultEntry(index=index, status="created"))
        except Exception as exc:  # noqa: BLE001 - deliberately broad: this
            # route's entire purpose is "report every row's outcome, never
            # let one row's failure abort the batch or crash the request" —
            # any exception from a single row's insert must be caught and
            # turned into that row's own error entry, not propagated.
            await current.session.rollback()
            results.append(
                CostCatalogItemBulkResultEntry(index=index, status="error", detail=str(exc))
            )

    return CostCatalogItemBulkResponse(results=results)
```

Note the `await current.session.rollback()` on a per-row exception: because this codebase's `get_current_user` dependency commits `current.session` once after the handler returns (Inherited Invariant #4), a failed row's exception would otherwise leave the session in a failed-transaction state that poisons every subsequent row's `flush()` too. Rolling back per-row keeps each row's outcome independent, exactly as promised.

Pydantic-level validation errors (empty `name`, non-numeric `unit_rate`, etc.) are actually caught EARLIER than this loop — `list[CostCatalogItemCreateRequest]` validates every row at request-parsing time, so a row failing `CostCatalogItemCreateRequest`'s own field constraints (e.g. `name: str = Field(..., min_length=1, ...)`) produces a 422 for the WHOLE request before this handler ever runs, not a per-row "error" entry. This means `test_bulk_import_partial_failure_reports_per_row`'s second row (`"name": ""`) will actually fail at the Pydantic layer, not inside this handler's try/except — **fix the test in Step 1** to use a row that passes schema validation but fails for a reason only detectable at insert time (there may be none for this model — `CostCatalogItemCreateRequest` has no DB-level uniqueness/FK constraint that could plausibly fail independently of Pydantic's own checks). If no such failure mode exists, rewrite that test to instead assert a 422 for a schema-invalid row (matching what actually happens) rather than a 200 with a per-row "error" entry — verify this by running the test against the real implementation and observing the actual response before finalizing either the test or a docstring claim about per-row error reporting.

- [ ] **Step 5: Run to verify it passes**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_estimation_gaps.py -k bulk_import -v`
Expected: PASS (after reconciling Step 1's test with the actual Pydantic-vs-runtime validation split noted in Step 4).

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/cost_catalog_item.py backend/app/routers/catalogs.py backend/tests/test_estimation_gaps.py
git commit -m "feat: catalog item bulk import"
```

## Task 8: Company branding — migration, model, schemas, routes

**Files:**
- Create: `backend/migrations/versions/0016_company_branding.py`
- Create: `backend/app/models/company_branding.py`
- Create: `backend/app/schemas/company_branding.py`
- Create: `backend/app/routers/branding.py`
- Modify: `backend/app/models/__init__.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/test_estimation_gaps.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_estimation_gaps.py`:

```python
@pytest.mark.asyncio
async def test_get_branding_defaults_when_no_row_exists(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        response = await client.get("/companies/branding")
        assert response.status_code == 200
        assert response.json()["logo_storage_path"] is None
        assert response.json()["accent_color"] == "#1e293b"
        assert response.json()["footer_text"] == ""


@pytest.mark.asyncio
async def test_put_branding_creates_and_updates(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        response = await client.put(
            "/companies/branding",
            json={"accent_color": "#ff0000", "footer_text": "Licensed & Insured"},
        )
        assert response.status_code == 200
        assert response.json()["accent_color"] == "#ff0000"

        response = await client.put(
            "/companies/branding", json={"accent_color": "#00ff00", "footer_text": "Updated"}
        )
        assert response.status_code == 200
        assert response.json()["accent_color"] == "#00ff00"

        get_response = await client.get("/companies/branding")
        assert get_response.json()["accent_color"] == "#00ff00"


@pytest.mark.asyncio
async def test_put_branding_forbidden_for_pm(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    # Adjust to this codebase's actual PM-creation helper (an invitation
    # accept flow, or a direct conftest fixture) — search conftest.py for
    # an existing "create a second user with role X" pattern used by
    # other role-gated tests before writing this.
    async with authed_client(async_client, admin_token, company_id) as client:
        response = await client.put(
            "/companies/branding", json={"accent_color": "#ff0000", "footer_text": ""}
        )
        assert response.status_code in (200, 403)  # 200 here is a placeholder
        # if this test is actually run as admin — rewrite using a real PM
        # session once the helper is confirmed, and assert exactly 403.


@pytest.mark.asyncio
async def test_upload_branding_logo(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        response = await client.post(
            "/companies/branding/logo",
            files={"file": ("logo.png", io.BytesIO(png_bytes), "image/png")},
        )
        assert response.status_code == 200
        assert response.json()["logo_storage_path"] is not None

        get_response = await client.get("/companies/branding")
        assert get_response.json()["logo_storage_path"] == response.json()["logo_storage_path"]


@pytest.mark.asyncio
async def test_upload_branding_logo_rejects_oversized_file(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        oversized = b"\x00" * (2 * 1024 * 1024 + 1)
        response = await client.post(
            "/companies/branding/logo",
            files={"file": ("logo.png", io.BytesIO(oversized), "image/png")},
        )
        assert response.status_code in (413, 422)


@pytest.mark.asyncio
async def test_upload_branding_logo_rejects_wrong_content_type(async_client):
    company_id, admin_token = await create_company_and_admin(async_client)
    async with authed_client(async_client, admin_token, company_id) as client:
        response = await client.post(
            "/companies/branding/logo",
            files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
        )
        assert response.status_code in (415, 422)
```

Before finalizing `test_put_branding_forbidden_for_pm`, check `tests/test_tier_gating.py` or `tests/conftest.py` for the established helper that creates a second, non-admin-role user in the same company (likely via the invitation flow) and rewrite that test to use it properly, asserting exactly `403`.

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_estimation_gaps.py -k branding -v`
Expected: FAIL (routes/table don't exist).

- [ ] **Step 3: Write the migration**

Create `backend/migrations/versions/0016_company_branding.py`:

```python
"""Company branding: logo, accent color, footer text applied to exported
Estimate PDFs.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-20

Per docs/superpowers/specs/2026-07-20-estimation-esignature-frontend-design.md
Decision 8. One row per company (not root-scoped like subscriptions —
branding is a per-company-branch setting, no inheritance concept), created
lazily on first PUT rather than at company-creation time (spec's own
"missing row = defaults" framing) — same "no row yet" pattern
integration_connections (migration 0013) already establishes for an
optional per-company settings table.

Plain, flat, company-scoped resource, no hierarchy/bidirectional concern of
its own — same standard, non-inherited tenant_isolation policy shape
migration 0013 gives integration_connections.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_branding",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id",
            UUID(as_uuid=True),
            sa.ForeignKey("companies.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("logo_storage_path", sa.Text, nullable=True),
        sa.Column("accent_color", sa.String(7), nullable=False, server_default="#1e293b"),
        sa.Column("footer_text", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.execute("ALTER TABLE company_branding ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON company_branding FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON company_branding")
    op.drop_table("company_branding")
```

- [ ] **Step 4: Run the migration**

Run: `cd backend && .venv\Scripts\python.exe -m alembic upgrade head`
Expected: `Running upgrade 0015 -> 0016, ...` with no errors.

- [ ] **Step 5: Write the model**

Create `backend/app/models/company_branding.py`:

```python
import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin, UpdatedAtMixin


class CompanyBranding(Base, UUIDPKMixin, UpdatedAtMixin):
    __tablename__ = "company_branding"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False, unique=True
    )
    logo_storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    accent_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#1e293b")
    footer_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
```

Add to `backend/app/models/__init__.py`: `from app.models.company_branding import CompanyBranding`, and add `"CompanyBranding"` to `__all__`.

- [ ] **Step 6: Write the schemas**

Create `backend/app/schemas/company_branding.py`:

```python
import uuid

from pydantic import BaseModel, ConfigDict, Field


class CompanyBrandingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    logo_storage_path: str | None
    accent_color: str
    footer_text: str


class CompanyBrandingPutRequest(BaseModel):
    accent_color: str = Field(..., pattern=r"^#[0-9a-fA-F]{6}$")
    footer_text: str = ""
```

- [ ] **Step 7: Add the logo storage helper**

In `backend/app/services/document_storage.py`, add after `write_esignature_artifact_file`:

```python
MAX_LOGO_SIZE_BYTES = 2 * 1024 * 1024
_ALLOWED_LOGO_CONTENT_TYPES = {"image/png": ".png", "image/jpeg": ".jpg"}


class UnsupportedLogoError(ValueError):
    """Raised for an oversized or wrong-content-type logo upload. Router
    call sites catch this and map it to 413/415."""


def write_company_logo_file(
    *, company_id: uuid.UUID, content_type: str, content: bytes
) -> str:
    """Writes a company's branding logo to
    `{settings.storage_root}/{company_id}/branding/logo{ext}` and returns the
    RELATIVE storage_path. Always overwrites (a company has exactly one
    current logo, same "no version history" reasoning
    `write_estimate_pdf_file` gives for estimate PDFs) — plain `"wb"` mode,
    not exclusive-create.

    Validates size and content type BEFORE writing anything to disk —
    same "reject outright" instinct this module applies everywhere else.
    """
    if len(content) > MAX_LOGO_SIZE_BYTES:
        raise UnsupportedLogoError(f"logo must not exceed {MAX_LOGO_SIZE_BYTES} bytes")
    if content_type not in _ALLOWED_LOGO_CONTENT_TYPES:
        raise UnsupportedLogoError("logo must be image/png or image/jpeg")

    ext = _ALLOWED_LOGO_CONTENT_TYPES[content_type]
    relative_path = f"{company_id}/branding/logo{ext}"
    absolute_path = Path(settings.storage_root) / str(company_id) / "branding" / f"logo{ext}"

    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_path.write_bytes(content)

    return relative_path
```

- [ ] **Step 8: Write the router**

Create `backend/app/routers/branding.py`:

```python
"""GET/PUT /companies/branding, POST /companies/branding/logo — spec
Decision 8. Admin-only for writes (logo/accent/footer are company identity,
narrower than the Estimation module's own admin+PM write convention);
admin+PM read (the PDF template tab is admin-only per the spec, but PM
still benefits from seeing current branding while building an estimate).
No tier gate — branding isn't part of MODULE_MIN_TIER's estimation-specific
feature set, it applies to any company regardless of tier.
"""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.models import CompanyBranding
from app.schemas.company_branding import CompanyBrandingPutRequest, CompanyBrandingResponse
from app.services.document_storage import UnsupportedLogoError, write_company_logo_file

router = APIRouter(prefix="/companies/branding", tags=["branding"])

_WRITE_ROLES = ("admin",)
_READ_ROLES = ("admin", "project_manager")


async def _get_or_create_branding(current: CurrentUser) -> CompanyBranding:
    result = await current.session.execute(
        select(CompanyBranding).where(CompanyBranding.company_id == current.company_id)
    )
    branding = result.scalar_one_or_none()
    if branding is None:
        branding = CompanyBranding(company_id=current.company_id)
        current.session.add(branding)
        await current.session.flush()
    return branding


@router.get("", response_model=CompanyBrandingResponse)
async def get_branding(
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> CompanyBrandingResponse:
    branding = await _get_or_create_branding(current)
    return CompanyBrandingResponse.model_validate(branding)


@router.put("", response_model=CompanyBrandingResponse)
async def put_branding(
    payload: CompanyBrandingPutRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> CompanyBrandingResponse:
    branding = await _get_or_create_branding(current)
    branding.accent_color = payload.accent_color
    branding.footer_text = payload.footer_text
    await current.session.flush()
    return CompanyBrandingResponse.model_validate(branding)


@router.post("/logo", response_model=CompanyBrandingResponse)
async def upload_branding_logo(
    file: UploadFile = File(...),
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> CompanyBrandingResponse:
    branding = await _get_or_create_branding(current)
    content = await file.read()

    try:
        relative_path = write_company_logo_file(
            company_id=current.company_id,
            content_type=file.content_type or "",
            content=content,
        )
    except UnsupportedLogoError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    branding.logo_storage_path = relative_path
    await current.session.flush()
    return CompanyBrandingResponse.model_validate(branding)
```

Register in `backend/app/main.py`: add `from app.routers import branding` to the existing import block and `app.include_router(branding.router)` alongside the other `include_router` calls.

- [ ] **Step 9: Run to verify it passes**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_estimation_gaps.py -k branding -v`
Expected: PASS (after adjusting the 413/415 vs 422 assertions in Step 1 to match the router's actual `HTTP_422_UNPROCESSABLE_ENTITY` — the tests above accept either; if you'd rather be precise, change `assert response.status_code in (413, 422)` to `assert response.status_code == 422` once confirmed).

- [ ] **Step 10: Commit**

```bash
git add backend/migrations/versions/0016_company_branding.py backend/app/models/company_branding.py backend/app/models/__init__.py backend/app/schemas/company_branding.py backend/app/routers/branding.py backend/app/services/document_storage.py backend/app/main.py backend/tests/test_estimation_gaps.py
git commit -m "feat: company branding - logo, accent color, footer text"
```

## Task 9: Full backend regression pass

**Files:** none (verification-only task).

- [ ] **Step 1: Run the full suite**

Redis for the test DB runs on a non-default port in this environment — see any prior session's notes in this repo's `docker-compose.override.yml` if present, or start a throwaway container: `docker run -d --name estimation-test-redis -p 16379:6379 redis:7-alpine` if one isn't already running.

Run:
```bash
cd backend
$env:REDIS_URL = "redis://localhost:16379/0"
.venv\Scripts\python.exe -m pytest -q
```

Expected: every test passes, including all of `test_estimation_gaps.py` plus the full pre-existing suite (no regression introduced by the new routes/migration). If anything outside `test_estimation_gaps.py` fails, treat it as a real regression from this plan's changes (e.g. a docstring in Step 4 of Task 4 flagged a possible missing `ON DELETE CASCADE` on `estimate_line_items` — a failure in `test_estimation.py` or similar around estimate deletion is where that would surface) and fix it before continuing; do not skip or comment out a failing test.

- [ ] **Step 2: No commit** — this task only verifies; nothing changes. If Step 1 required a fix, that fix gets its own commit under whichever earlier task it belongs to (amend that task's own commit only if you're still working within the same uncommitted step; otherwise a small follow-up commit is fine).

## Task 10: Frontend API types regeneration

**Files:**
- Modify: `frontend/lib/api/types.ts` (regenerated, not hand-edited)

- [ ] **Step 1: Bring up the worktree's Docker stack**

This plan's worktree is `D:\Development\New const proj mgt software\.worktrees\estimation-esignature`. Before running this task, stop whatever Docker Compose stack is currently running against the MAIN repo checkout (`docker compose down` from `D:\Development\New const proj mgt software`) — only one stack should bind the shared host ports at a time. Then, from the worktree root:

```bash
cd "D:\Development\New const proj mgt software\.worktrees\estimation-esignature"
docker compose up -d --build
```

If a `docker-compose.override.yml` remapping the Redis host port exists in the main repo (check for one — Windows may reserve TCP 6280–6479, causing a bind failure on the default 6379), copy it into this worktree before bringing the stack up.

Wait for the backend container to report healthy, then confirm migrations are at head:

```bash
docker compose exec backend alembic current
```

Expected: `0016 (head)`. If the target Postgres volume is behind, run `docker compose exec backend alembic upgrade head` (using the container's own `MIGRATIONS_DATABASE_URL`/hostname convention — check the worktree's `.env` for the exact variable, matching whatever prior sessions in this repo have documented for this exact command).

- [ ] **Step 2: Regenerate**

```bash
cd frontend
npm run generate:api-types
```

Expected: `lib/api/types.ts` is rewritten with no errors. Verify every new backend path is present — grep the file for: `/estimates/{estimate_id}/pdf`, `/estimates/{estimate_id}/calculate`, `/catalogs/items/{item_id}`, `/catalogs/items/bulk`, `/markup-profiles/{profile_id}`, `/change-orders/{change_order_id}`, `/change-orders`, `/companies/branding`, `/companies/branding/logo`.

- [ ] **Step 3: Type-check**

```bash
npx tsc --noEmit
```

Expected: exit 0 (nothing in the frontend references the new types yet, so this only confirms the generated file itself is syntactically valid TypeScript). Delete any stray `tsconfig.tsbuildinfo` before committing.

- [ ] **Step 4: Tear down the worktree stack, restore main**

```bash
cd "D:\Development\New const proj mgt software\.worktrees\estimation-esignature"
docker compose down
cd "D:\Development\New const proj mgt software"
docker compose up -d
```

- [ ] **Step 5: Commit**

```bash
cd "D:\Development\New const proj mgt software\.worktrees\estimation-esignature"
git add frontend/lib/api/types.ts
git commit -m "feat: regenerate API types for the estimation and e-signature endpoints"
```

## Task 11: CSV helper + middleware matcher

**Files:**
- Create: `frontend/lib/csv.ts`
- Modify: `frontend/middleware.ts`

- [ ] **Step 1: Write `lib/csv.ts`**

```typescript
// Hand-rolled parser/serializer for the catalog import/export format:
// four columns, header row required — category,name,unit,unit_rate.
// No library: this format has no embedded commas beyond what basic quote
// handling below covers, and pulling in a CSV dependency for four columns
// is unwarranted (spec Decision 9).

export interface CatalogCsvRow {
  category: string;
  name: string;
  unit: string;
  unit_rate: string;
}

const EXPECTED_HEADER = ["category", "name", "unit", "unit_rate"];

export class CsvParseError extends Error {}

function parseLine(line: string): string[] {
  const fields: string[] = [];
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const char = line[i];
    if (inQuotes) {
      if (char === '"' && line[i + 1] === '"') {
        current += '"';
        i++;
      } else if (char === '"') {
        inQuotes = false;
      } else {
        current += char;
      }
    } else if (char === '"') {
      inQuotes = true;
    } else if (char === ",") {
      fields.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  fields.push(current);
  return fields;
}

export function parseCatalogCsv(text: string): CatalogCsvRow[] {
  const lines = text.replace(/\r\n/g, "\n").split("\n").filter((l) => l.trim() !== "");
  if (lines.length === 0) throw new CsvParseError("File is empty");

  const header = parseLine(lines[0]).map((h) => h.trim().toLowerCase());
  if (header.length !== 4 || EXPECTED_HEADER.some((col, i) => header[i] !== col)) {
    throw new CsvParseError(`Header must be exactly: ${EXPECTED_HEADER.join(",")}`);
  }

  return lines.slice(1).map((line, index) => {
    const fields = parseLine(line);
    if (fields.length !== 4) {
      throw new CsvParseError(`Row ${index + 2} has ${fields.length} columns, expected 4`);
    }
    const [category, name, unit, unit_rate] = fields;
    return { category: category.trim(), name: name.trim(), unit: unit.trim(), unit_rate: unit_rate.trim() };
  });
}

function escapeCsvField(value: string): string {
  if (value.includes(",") || value.includes('"') || value.includes("\n")) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

export function serializeCatalogCsv(rows: CatalogCsvRow[]): string {
  const lines = [EXPECTED_HEADER.join(",")];
  for (const row of rows) {
    lines.push(
      [row.category, row.name, row.unit, row.unit_rate].map(escapeCsvField).join(",")
    );
  }
  return lines.join("\n");
}
```

- [ ] **Step 2: Update the middleware matcher**

Read `frontend/middleware.ts` first. Find the `matcher` array in its `config` export (currently something like `["/dashboard/:path*", "/account/:path*", "/leads/:path*", "/projects/:path*", "/my-tasks/:path*"]` — confirm the exact current array before editing) and add `"/estimates/:path*"` and `"/catalog/:path*"` to it.

- [ ] **Step 3: Type-check**

```bash
cd frontend
npx tsc --noEmit
```
Expected: exit 0.

- [ ] **Step 4: Commit**

```bash
git add frontend/lib/csv.ts frontend/middleware.ts
git commit -m "feat: catalog CSV helper and middleware route matcher"
```

## Task 12: Typed e-signature components

**Files:**
- Create: `frontend/components/esign/TypedSignature.tsx`
- Create: `frontend/components/esign/SigningPanel.tsx`

- [ ] **Step 1: Write `TypedSignature.tsx`**

```tsx
"use client";

import * as React from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

// DocuSign-style "adopt a signature": the signer types their name, sees it
// rendered in a script font, and on submit that rendering is drawn to a
// hidden canvas and exported as a PNG blob — the exact artifact shape the
// backend's approve routes expect (multipart signature_artifact file).
export function TypedSignature({
  onSign,
  submitting,
}: {
  onSign: (args: { signerName: string; signerEmail: string; artifact: Blob }) => void;
  submitting: boolean;
}) {
  const [signerName, setSignerName] = React.useState("");
  const [signerEmail, setSignerEmail] = React.useState("");
  const canvasRef = React.useRef<HTMLCanvasElement | null>(null);

  function renderToCanvas(name: string): void {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#1e293b";
    ctx.font = "40px 'Brush Script MT', cursive";
    ctx.textBaseline = "middle";
    ctx.fillText(name, 16, canvas.height / 2);
  }

  React.useEffect(() => {
    renderToCanvas(signerName || " ");
  }, [signerName]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !signerName.trim() || !signerEmail.trim()) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    renderToCanvas(signerName);
    const artifact = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/png"));
    if (!artifact) return;
    onSign({ signerName, signerEmail, artifact });
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="signer-name">Full name</Label>
        <Input
          id="signer-name"
          value={signerName}
          onChange={(e) => setSignerName(e.target.value)}
          disabled={submitting}
          required
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="signer-email">Email</Label>
        <Input
          id="signer-email"
          type="email"
          value={signerEmail}
          onChange={(e) => setSignerEmail(e.target.value)}
          disabled={submitting}
          required
        />
      </div>
      <div className="border border-slate-200 rounded-md p-2">
        <p className="text-xs text-slate-500 mb-1">Signature preview</p>
        <canvas ref={canvasRef} width={320} height={80} className="w-full h-20" />
      </div>
      <Button type="submit" disabled={submitting || !signerName.trim() || !signerEmail.trim()}>
        {submitting ? "Signing…" : "Approve & sign"}
      </Button>
    </form>
  );
}
```

- [ ] **Step 2: Write `SigningPanel.tsx`**

```tsx
"use client";

import * as React from "react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { TypedSignature } from "./TypedSignature";

// Shared approve/reject panel used by both the estimate detail page (sent
// state) and the client's inline change-order card. `approveUrl`/`rejectUrl`
// are the BFF routes to POST to — the two callers point this at different
// endpoints but the interaction is identical.
export function SigningPanel({
  approveUrl,
  rejectUrl,
  accessToken,
  onDone,
}: {
  approveUrl: string;
  rejectUrl: string;
  accessToken: string;
  onDone: () => void;
}) {
  const [mode, setMode] = React.useState<"choose" | "reject">("choose");
  const [reason, setReason] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function handleApprove({
    signerName,
    signerEmail,
    artifact,
  }: {
    signerName: string;
    signerEmail: string;
    artifact: Blob;
  }) {
    setError(null);
    setSubmitting(true);
    try {
      const formData = new FormData();
      formData.append("signer_name", signerName);
      formData.append("signer_email", signerEmail);
      formData.append("signature_artifact", artifact, "signature.png");
      const response = await fetch(approveUrl, {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
        body: formData,
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to approve");
        return;
      }
      onDone();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleReject(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !reason.trim()) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch(rejectUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ reason }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to reject");
        return;
      }
      onDone();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col gap-3 border border-slate-200 rounded-md p-4">
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {mode === "choose" && (
        <>
          <TypedSignature onSign={handleApprove} submitting={submitting} />
          <button
            type="button"
            onClick={() => setMode("reject")}
            disabled={submitting}
            className="text-sm text-slate-500 hover:underline self-start"
          >
            Reject instead
          </button>
        </>
      )}
      {mode === "reject" && (
        <form onSubmit={handleReject} className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="reject-reason">Reason for rejecting</Label>
            <Textarea
              id="reject-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              disabled={submitting}
              required
            />
          </div>
          <div className="flex gap-2">
            <Button type="submit" variant="outline" disabled={submitting || !reason.trim()}>
              {submitting ? "Submitting…" : "Reject"}
            </Button>
            <button
              type="button"
              onClick={() => setMode("choose")}
              disabled={submitting}
              className="text-sm text-slate-500 hover:underline"
            >
              Back
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Type-check**

```bash
cd frontend
npx tsc --noEmit
```
Expected: exit 0.

- [ ] **Step 4: Commit**

```bash
git add frontend/components/esign
git commit -m "feat: typed e-signature capture and approve/reject panel"
```

## Task 13: BFF Route Handlers — estimates

**Files:**
- Create: `frontend/app/(app)/api/estimates/route.ts`
- Create: `frontend/app/(app)/api/estimates/[id]/route.ts`
- Create: `frontend/app/(app)/api/estimates/[id]/lines/route.ts`
- Create: `frontend/app/(app)/api/estimates/[id]/calculate/route.ts`
- Create: `frontend/app/(app)/api/estimates/[id]/export/route.ts`
- Create: `frontend/app/(app)/api/estimates/[id]/pdf/route.ts`
- Create: `frontend/app/(app)/api/estimates/[id]/send-for-signature/route.ts`
- Create: `frontend/app/(app)/api/estimates/[id]/approve/route.ts`
- Create: `frontend/app/(app)/api/estimates/[id]/reject/route.ts`
- Create: `frontend/app/(app)/api/esignatures/[id]/route.ts`

All handlers follow the established `bearerToken → apiFetch → errorResponse` pattern (see `frontend/app/(app)/api/leads/[id]/route.ts` for the exact reference). `pdf/route.ts` streams raw bytes (same pattern as the existing document-download handler) rather than going through `apiFetch`; `approve/route.ts` passes multipart through directly (same pattern as the existing document-upload handler).

- [ ] **Step 1: `estimates/route.ts`**

```typescript
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  try {
    const data = await apiFetch("/estimates", "get", {
      accessToken: token,
      query: {
        status: request.nextUrl.searchParams.get("status") ?? undefined,
        cursor: request.nextUrl.searchParams.get("cursor") ?? undefined,
      },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load estimates");
  }
}

export async function POST(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const body = await request.json();
  try {
    const data = await apiFetch("/estimates", "post", { accessToken: token, body });
    return NextResponse.json(data, { status: 201 });
  } catch (err) {
    return errorResponse(err, "Failed to create estimate");
  }
}
```

- [ ] **Step 2: `estimates/[id]/route.ts`**

```typescript
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    const data = await apiFetch("/estimates/{estimate_id}", "get", {
      accessToken: token,
      params: { estimate_id: id },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load estimate");
  }
}

export async function PATCH(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  const body = await request.json();
  try {
    const data = await apiFetch("/estimates/{estimate_id}", "patch", {
      accessToken: token,
      params: { estimate_id: id },
      body,
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to update estimate");
  }
}

export async function DELETE(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    await apiFetch("/estimates/{estimate_id}", "delete", {
      accessToken: token,
      params: { estimate_id: id },
    });
    return new NextResponse(null, { status: 204 });
  } catch (err) {
    return errorResponse(err, "Failed to delete estimate");
  }
}
```

- [ ] **Step 3: `estimates/[id]/lines/route.ts`**

```typescript
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function PUT(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  const body = await request.json();
  try {
    const data = await apiFetch("/estimates/{estimate_id}/lines", "put", {
      accessToken: token,
      params: { estimate_id: id },
      body,
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to save line items");
  }
}
```

- [ ] **Step 4: `estimates/[id]/calculate/route.ts`, `export/route.ts`, `send-for-signature/route.ts`**

Three near-identical no-body POST handlers. `calculate/route.ts`:

```typescript
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    const data = await apiFetch("/estimates/{estimate_id}/calculate", "post", {
      accessToken: token,
      params: { estimate_id: id },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to calculate estimate");
  }
}
```

`export/route.ts` — identical shape, path `/estimates/{estimate_id}/export`, fallback message `"Failed to export estimate"`, and `NextResponse.json(data, { status: 202 })` (matching the backend's 202).

`send-for-signature/route.ts` — identical shape, path `/estimates/{estimate_id}/send-for-signature`, fallback message `"Failed to send estimate for signature"`, plain 200.

- [ ] **Step 5: `estimates/[id]/pdf/route.ts`**

```typescript
import { NextRequest, NextResponse } from "next/server";
import { BACKEND_API_URL } from "@/lib/api/client";
import { bearerToken, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    const upstream = await fetch(`${BACKEND_API_URL}/estimates/${encodeURIComponent(id)}/pdf`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!upstream.ok) {
      let detail = "PDF not available";
      try {
        detail = (await upstream.json()).detail ?? detail;
      } catch {}
      return NextResponse.json({ detail }, { status: upstream.status });
    }
    return new NextResponse(upstream.body, {
      headers: {
        "Content-Type": upstream.headers.get("Content-Type") ?? "application/pdf",
        "Content-Disposition": upstream.headers.get("Content-Disposition") ?? "attachment",
      },
    });
  } catch {
    return NextResponse.json({ detail: "PDF not available" }, { status: 502 });
  }
}
```

- [ ] **Step 6: `estimates/[id]/approve/route.ts`**

```typescript
import { NextRequest, NextResponse } from "next/server";
import { BACKEND_API_URL } from "@/lib/api/client";
import { bearerToken, missingTokenResponse } from "@/lib/api/handler-utils";

export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  const formData = await request.formData();
  try {
    const response = await fetch(`${BACKEND_API_URL}/estimates/${encodeURIComponent(id)}/approve`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: formData,
    });
    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch {
    return NextResponse.json({ detail: "Failed to approve estimate" }, { status: 502 });
  }
}
```

- [ ] **Step 7: `estimates/[id]/reject/route.ts`**

```typescript
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  const body = await request.json();
  try {
    const data = await apiFetch("/estimates/{estimate_id}/reject", "post", {
      accessToken: token,
      params: { estimate_id: id },
      body,
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to reject estimate");
  }
}
```

- [ ] **Step 8: `esignatures/[id]/route.ts`**

```typescript
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    const data = await apiFetch("/esignatures/{esignature_id}", "get", {
      accessToken: token,
      params: { esignature_id: id },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load signature record");
  }
}
```

- [ ] **Step 9: Type-check**

```bash
cd frontend
npx tsc --noEmit
```
Expected: exit 0 — every path string above must match a key in the regenerated `lib/api/types.ts` from Task 10, or this fails immediately. If a path key is rejected, STOP and report rather than casting around it — it means Task 10's regeneration or one of the backend route paths in Tasks 1–8 doesn't match what's written here.

- [ ] **Step 10: Commit**

```bash
git add "frontend/app/(app)/api/estimates" "frontend/app/(app)/api/esignatures"
git commit -m "feat: estimates and esignatures BFF route handlers"
```

## Task 14: BFF Route Handlers — catalog, markup profiles, branding

**Files:**
- Create: `frontend/app/(app)/api/catalog/items/route.ts`
- Create: `frontend/app/(app)/api/catalog/items/bulk/route.ts`
- Create: `frontend/app/(app)/api/catalog/items/[id]/route.ts`
- Create: `frontend/app/(app)/api/catalog/items/[id]/override/route.ts`
- Create: `frontend/app/(app)/api/markup-profiles/route.ts`
- Create: `frontend/app/(app)/api/markup-profiles/[id]/route.ts`
- Create: `frontend/app/(app)/api/companies/branding/route.ts`
- Create: `frontend/app/(app)/api/companies/branding/logo/route.ts`

- [ ] **Step 1: `catalog/items/route.ts`**

```typescript
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  try {
    const data = await apiFetch("/catalogs/items", "get", {
      accessToken: token,
      query: {
        category: request.nextUrl.searchParams.get("category") ?? undefined,
        search: request.nextUrl.searchParams.get("search") ?? undefined,
        cursor: request.nextUrl.searchParams.get("cursor") ?? undefined,
      },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load catalog items");
  }
}

export async function POST(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const body = await request.json();
  try {
    const data = await apiFetch("/catalogs/items", "post", { accessToken: token, body });
    return NextResponse.json(data, { status: 201 });
  } catch (err) {
    return errorResponse(err, "Failed to create catalog item");
  }
}
```

- [ ] **Step 2: `catalog/items/bulk/route.ts`**

```typescript
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function POST(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const body = await request.json();
  try {
    const data = await apiFetch("/catalogs/items/bulk", "post", { accessToken: token, body });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to import catalog items");
  }
}
```

- [ ] **Step 3: `catalog/items/[id]/route.ts`**

```typescript
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function PATCH(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  const body = await request.json();
  try {
    const data = await apiFetch("/catalogs/items/{item_id}", "patch", {
      accessToken: token,
      params: { item_id: id },
      body,
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to update catalog item");
  }
}

export async function DELETE(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    await apiFetch("/catalogs/items/{item_id}", "delete", {
      accessToken: token,
      params: { item_id: id },
    });
    return new NextResponse(null, { status: 204 });
  } catch (err) {
    return errorResponse(err, "Failed to delete catalog item");
  }
}
```

- [ ] **Step 4: `catalog/items/[id]/override/route.ts`**

```typescript
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  const body = await request.json();
  try {
    const data = await apiFetch("/catalogs/items/{parent_catalog_item_id}/override", "post", {
      accessToken: token,
      params: { parent_catalog_item_id: id },
      body,
    });
    return NextResponse.json(data, { status: 201 });
  } catch (err) {
    return errorResponse(err, "Failed to create catalog item override");
  }
}
```

- [ ] **Step 5: `markup-profiles/route.ts` and `markup-profiles/[id]/route.ts`**

`markup-profiles/route.ts` — same GET/POST shape as `catalog/items/route.ts`, path `/markup-profiles`, query only `cursor`, fallback messages "Failed to load markup profiles" / "Failed to create markup profile".

`markup-profiles/[id]/route.ts` — same PATCH/DELETE shape as `catalog/items/[id]/route.ts`, path `/markup-profiles/{profile_id}`, param key `profile_id`, fallback messages "Failed to update markup profile" / "Failed to delete markup profile".

- [ ] **Step 6: `companies/branding/route.ts`**

```typescript
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  try {
    const data = await apiFetch("/companies/branding", "get", { accessToken: token });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load branding");
  }
}

export async function PUT(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const body = await request.json();
  try {
    const data = await apiFetch("/companies/branding", "put", { accessToken: token, body });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to save branding");
  }
}
```

- [ ] **Step 7: `companies/branding/logo/route.ts`**

```typescript
import { NextRequest, NextResponse } from "next/server";
import { BACKEND_API_URL } from "@/lib/api/client";
import { bearerToken, missingTokenResponse } from "@/lib/api/handler-utils";

export async function POST(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const formData = await request.formData();
  try {
    const response = await fetch(`${BACKEND_API_URL}/companies/branding/logo`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: formData,
    });
    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch {
    return NextResponse.json({ detail: "Failed to upload logo" }, { status: 502 });
  }
}
```

- [ ] **Step 8: Type-check**

```bash
cd frontend
npx tsc --noEmit
```
Expected: exit 0.

- [ ] **Step 9: Commit**

```bash
git add "frontend/app/(app)/api/catalog" "frontend/app/(app)/api/markup-profiles" "frontend/app/(app)/api/companies"
git commit -m "feat: catalog, markup profile, and branding BFF route handlers"
```

## Task 15: BFF Route Handlers — change orders

**Files:**
- Create: `frontend/app/(app)/api/projects/[id]/change-orders/route.ts`
- Create: `frontend/app/(app)/api/change-orders/route.ts`
- Create: `frontend/app/(app)/api/change-orders/[id]/route.ts`
- Create: `frontend/app/(app)/api/change-orders/[id]/send-for-signature/route.ts`
- Create: `frontend/app/(app)/api/change-orders/[id]/approve/route.ts`
- Create: `frontend/app/(app)/api/change-orders/[id]/reject/route.ts`

- [ ] **Step 1: `projects/[id]/change-orders/route.ts`**

```typescript
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    const data = await apiFetch("/projects/{project_id}/change-orders", "get", {
      accessToken: token,
      params: { project_id: id },
      query: { cursor: request.nextUrl.searchParams.get("cursor") ?? undefined },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load change orders");
  }
}

export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  const body = await request.json();
  try {
    const data = await apiFetch("/projects/{project_id}/change-orders", "post", {
      accessToken: token,
      params: { project_id: id },
      body,
    });
    return NextResponse.json(data, { status: 201 });
  } catch (err) {
    return errorResponse(err, "Failed to create change order");
  }
}
```

- [ ] **Step 2: `change-orders/route.ts`**

```typescript
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  try {
    const data = await apiFetch("/change-orders", "get", {
      accessToken: token,
      query: {
        status: request.nextUrl.searchParams.get("status") ?? undefined,
        cursor: request.nextUrl.searchParams.get("cursor") ?? undefined,
      },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load change orders");
  }
}
```

- [ ] **Step 3: `change-orders/[id]/route.ts`**

```typescript
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    const data = await apiFetch("/change-orders/{change_order_id}", "get", {
      accessToken: token,
      params: { change_order_id: id },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load change order");
  }
}
```

- [ ] **Step 4: `change-orders/[id]/send-for-signature/route.ts`**

```typescript
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    const data = await apiFetch("/change-orders/{change_order_id}/send-for-signature", "post", {
      accessToken: token,
      params: { change_order_id: id },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to send change order for signature");
  }
}
```

- [ ] **Step 5: `change-orders/[id]/approve/route.ts`**

Same multipart pass-through shape as `estimates/[id]/approve/route.ts` (Task 13, Step 6), pointed at `${BACKEND_API_URL}/change-orders/${encodeURIComponent(id)}/approve`.

- [ ] **Step 6: `change-orders/[id]/reject/route.ts`**

Same JSON-body shape as `estimates/[id]/reject/route.ts` (Task 13, Step 7), path `/change-orders/{change_order_id}/reject`, param key `change_order_id`, fallback message "Failed to reject change order".

- [ ] **Step 7: Type-check**

```bash
cd frontend
npx tsc --noEmit
```
Expected: exit 0.

- [ ] **Step 8: Commit**

```bash
git add "frontend/app/(app)/api/projects" "frontend/app/(app)/api/change-orders"
git commit -m "feat: change order BFF route handlers"
```

## Task 16: Estimates global list + create page + Nav links

**Files:**
- Create: `frontend/app/(app)/estimates/page.tsx`
- Create: `frontend/components/estimates/NewEstimateForm.tsx`
- Create: `frontend/app/(app)/estimates/new/page.tsx`
- Modify: `frontend/components/app-shell/Nav.tsx`

- [ ] **Step 1: `estimates/page.tsx`**

Follow `frontend/app/(app)/leads/page.tsx`'s exact structure (read it first): request-generation-guarded cursor pagination, status filter, empty state, Load more.

```tsx
"use client";

import * as React from "react";
import Link from "next/link";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatCurrency } from "@/lib/format";

const ESTIMATE_STATUSES = ["draft", "sent", "approved", "rejected"] as const;

interface EstimateRow {
  id: string;
  status: string;
  total: string | null;
  parent_name: string | null;
}

export default function EstimatesPage() {
  const { accessToken, role } = useAuth();
  const [estimates, setEstimates] = React.useState<EstimateRow[]>([]);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [statusFilter, setStatusFilter] = React.useState("");
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const requestGenRef = React.useRef(0);

  const canCreate = role === "admin" || role === "project_manager";

  const load = React.useCallback(
    async (cursor: string | null, replace: boolean) => {
      if (!accessToken) return;
      const generation = replace ? ++requestGenRef.current : requestGenRef.current;
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams();
        if (statusFilter) params.set("status", statusFilter);
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/estimates?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (generation !== requestGenRef.current) return;
        if (!response.ok) {
          setError(data.detail ?? "Failed to load estimates");
          return;
        }
        setEstimates((prev) => (replace ? data.items : [...prev, ...data.items]));
        setNextCursor(data.next_cursor);
      } catch {
        if (generation === requestGenRef.current) {
          setError("Unable to reach the server. Check your connection and try again.");
        }
      } finally {
        if (generation === requestGenRef.current) setLoading(false);
      }
    },
    [accessToken, statusFilter]
  );

  React.useEffect(() => {
    void Promise.resolve().then(() => load(null, true));
  }, [load]);

  return (
    <main className="p-6 flex flex-col gap-4 max-w-3xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Estimates</h1>
        {canCreate && (
          <Link href="/estimates/new">
            <Button>New estimate</Button>
          </Link>
        )}
      </div>
      <Select
        aria-label="Filter by status"
        className="w-44"
        value={statusFilter}
        onChange={(e) => setStatusFilter(e.target.value)}
      >
        <option value="">All statuses</option>
        {ESTIMATE_STATUSES.map((s) => (
          <option key={s} value={s}>
            {s[0].toUpperCase() + s.slice(1)}
          </option>
        ))}
      </Select>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {!loading && estimates.length === 0 && !error && (
        <p className="text-sm text-slate-600">No estimates yet — create your first estimate.</p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {estimates.map((estimate) => (
          <li key={estimate.id}>
            <Link
              href={`/estimates/${estimate.id}`}
              className="flex items-center gap-4 px-4 py-3 hover:bg-slate-50"
            >
              <span className="flex-1 text-sm font-medium">{estimate.parent_name ?? "—"}</span>
              <span className="text-sm text-slate-600">{formatCurrency(estimate.total)}</span>
              <StatusBadge status={estimate.status} />
            </Link>
          </li>
        ))}
      </ul>
      {nextCursor && (
        <Button variant="outline" onClick={() => load(nextCursor, false)} disabled={loading}>
          Load more
        </Button>
      )}
    </main>
  );
}
```

`StatusBadge` (`frontend/components/ui/status-badge.tsx`) falls back to `slate` tone and the raw status string via `labelFor` for any status not in its `STATUS_TONES`/`STATUS_LABELS` maps (`draft`/`sent`/`approved`/`rejected` aren't in either yet) — this task does not need to touch that file; it degrades gracefully. If a later task wants proper tones/labels for these, add `draft: "slate", sent: "amber", approved: "green", rejected: "red"` to `STATUS_TONES` and matching entries to `STATUS_LABELS` in `frontend/lib/state-machines.ts` at that point — optional polish, not required for this task's tests to pass.

- [ ] **Step 2: `NewEstimateForm.tsx`**

A form that can be pre-bound to a project or lead id (used standalone on `/estimates/new` and embedded from the project/lead pages in later tasks):

```tsx
"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";

interface MarkupProfileOption {
  id: string;
  name: string;
}

export function NewEstimateForm({
  projectId,
  leadId,
}: {
  projectId?: string;
  leadId?: string;
}) {
  const router = useRouter();
  const { accessToken } = useAuth();
  const [profiles, setProfiles] = React.useState<MarkupProfileOption[]>([]);
  const [markupProfileId, setMarkupProfileId] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const loadProfiles = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const response = await fetch("/api/markup-profiles", {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (response.ok) setProfiles(data.items);
    } catch {
      // Non-blocking — the Select just stays empty if this fails.
    }
  }, [accessToken]);

  React.useEffect(() => {
    void Promise.resolve().then(() => loadProfiles());
  }, [loadProfiles]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken || !markupProfileId) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch("/api/estimates", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          project_id: projectId ?? null,
          lead_id: leadId ?? null,
          markup_profile_id: markupProfileId,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to create estimate");
        return;
      }
      router.push(`/estimates/${data.id}`);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4 w-full max-w-sm">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="new-estimate-markup">Markup profile</Label>
        <Select
          id="new-estimate-markup"
          value={markupProfileId}
          onChange={(e) => setMarkupProfileId(e.target.value)}
          disabled={submitting}
          required
        >
          <option value="">Select…</option>
          {profiles.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </Select>
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      <Button type="submit" disabled={submitting || !markupProfileId}>
        {submitting ? "Creating…" : "Create estimate"}
      </Button>
    </form>
  );
}
```

- [ ] **Step 3: `estimates/new/page.tsx`**

Standalone create page, reads `?project_id=` / `?lead_id=` search params for pre-binding from the project tab / lead section (later tasks):

```tsx
"use client";

import { useSearchParams } from "next/navigation";
import { NewEstimateForm } from "@/components/estimates/NewEstimateForm";

export default function NewEstimatePage() {
  const params = useSearchParams();
  const projectId = params.get("project_id") ?? undefined;
  const leadId = params.get("lead_id") ?? undefined;

  return (
    <main className="p-6">
      <h1 className="text-xl font-semibold mb-4">New estimate</h1>
      <NewEstimateForm projectId={projectId} leadId={leadId} />
    </main>
  );
}
```

- [ ] **Step 4: Nav links**

In `frontend/components/app-shell/Nav.tsx`, add after the existing Projects link:

```tsx
        {(role === "admin" || role === "project_manager" || role === "accountant") && (
          <Link href="/estimates" className="text-sm text-slate-600 hover:text-slate-900">
            Estimates
          </Link>
        )}
        {(role === "admin" || role === "project_manager") && (
          <Link href="/catalog" className="text-sm text-slate-600 hover:text-slate-900">
            Catalog
          </Link>
        )}
```

- [ ] **Step 5: Type-check**

```bash
cd frontend
npx tsc --noEmit
```
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add "frontend/app/(app)/estimates" frontend/components/estimates/NewEstimateForm.tsx frontend/components/app-shell/Nav.tsx
git commit -m "feat: estimates list and create screens, nav links"
```

## Task 17: Estimate builder (draft state)

**Files:**
- Create: `frontend/components/estimates/CatalogPanel.tsx`
- Create: `frontend/components/estimates/LineRows.tsx`
- Create: `frontend/components/estimates/EstimateBuilder.tsx`

- [ ] **Step 1: `CatalogPanel.tsx`**

Browsable/searchable catalog, grouped by category, each row has a **+** to add:

```tsx
"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { formatCurrency } from "@/lib/format";

interface CatalogItem {
  id: string;
  category: string;
  name: string;
  unit: string;
  unit_rate: string;
}

export function CatalogPanel({ onAdd }: { onAdd: (item: CatalogItem) => void }) {
  const { accessToken } = useAuth();
  const [items, setItems] = React.useState<CatalogItem[]>([]);
  const [search, setSearch] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);

  const loadAll = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      // Follows next_cursor to exhaustion — the catalog panel needs the
      // whole browsable set, not one page (same pagination-completeness
      // reasoning the CRM+PM tabs settled on for lists a user must see in
      // full).
      const all: CatalogItem[] = [];
      let cursor: string | null = null;
      do {
        const params = new URLSearchParams();
        if (search) params.set("search", search);
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/catalog/items?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (!response.ok) {
          setError(data.detail ?? "Failed to load catalog");
          return;
        }
        all.push(...data.items);
        cursor = data.next_cursor ?? null;
      } while (cursor);
      setItems(all);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, search]);

  React.useEffect(() => {
    void Promise.resolve().then(() => loadAll());
  }, [loadAll]);

  const grouped = React.useMemo(() => {
    const groups = new Map<string, CatalogItem[]>();
    for (const item of items) {
      const list = groups.get(item.category) ?? [];
      list.push(item);
      groups.set(item.category, list);
    }
    return groups;
  }, [items]);

  return (
    <div className="flex flex-col gap-3">
      <Input
        aria-label="Search catalog"
        placeholder="Search catalog…"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      <div className="flex flex-col gap-3 max-h-96 overflow-y-auto">
        {Array.from(grouped.entries()).map(([category, categoryItems]) => (
          <div key={category}>
            <p className="text-xs uppercase text-slate-500 font-medium mb-1">{category}</p>
            {categoryItems.map((item) => (
              <div key={item.id} className="flex items-center gap-2 py-1 text-sm">
                <span className="flex-1">
                  {item.name} · {formatCurrency(item.unit_rate)}/{item.unit}
                </span>
                <Button type="button" size="sm" variant="outline" onClick={() => onAdd(item)}>
                  +
                </Button>
              </div>
            ))}
          </div>
        ))}
        {items.length === 0 && !error && (
          <p className="text-sm text-slate-500">No catalog items yet.</p>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: `LineRows.tsx`**

```tsx
"use client";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { formatCurrency } from "@/lib/format";

export interface DraftLine {
  cost_catalog_item_id: string;
  name: string;
  unit: string;
  unit_rate: string;
  quantity: string;
}

export function LineRows({
  lines,
  onQuantityChange,
  onRemove,
}: {
  lines: DraftLine[];
  onQuantityChange: (costCatalogItemId: string, quantity: string) => void;
  onRemove: (costCatalogItemId: string) => void;
}) {
  const subtotal = lines.reduce(
    (sum, line) => sum + Number(line.quantity || 0) * Number(line.unit_rate || 0),
    0
  );

  return (
    <div className="flex flex-col gap-2">
      {lines.length === 0 && <p className="text-sm text-slate-500">No line items yet — add some from the catalog.</p>}
      {lines.map((line) => (
        <div key={line.cost_catalog_item_id} className="flex items-center gap-2 text-sm">
          <span className="flex-1">{line.name}</span>
          <Input
            aria-label={`Quantity for ${line.name}`}
            type="number"
            min="0"
            step="any"
            className="w-24 h-8"
            value={line.quantity}
            onChange={(e) => onQuantityChange(line.cost_catalog_item_id, e.target.value)}
          />
          <span className="w-16 text-slate-500">{line.unit}</span>
          <span className="w-24 text-right">
            {formatCurrency(Number(line.quantity || 0) * Number(line.unit_rate || 0))}
          </span>
          <button
            type="button"
            onClick={() => onRemove(line.cost_catalog_item_id)}
            className="text-slate-400 hover:text-red-600"
            aria-label={`Remove ${line.name}`}
          >
            ✕
          </button>
        </div>
      ))}
      <div className="border-t border-slate-200 pt-2 flex justify-between text-sm font-medium">
        <span>Subtotal (before markup)</span>
        <span>{formatCurrency(subtotal)}</span>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: `EstimateBuilder.tsx`**

The draft-state container: catalog panel + line rows + save (chains lines-replace → calculate) + category breakdown.

```tsx
"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { CatalogPanel } from "./CatalogPanel";
import { LineRows, DraftLine } from "./LineRows";
import { formatCurrency } from "@/lib/format";

interface ExistingLineItem {
  cost_catalog_item_id: string;
  quantity: string;
  unit_rate_snapshot: string;
}

interface CategorySubtotal {
  category: string;
  subtotal: string;
}

export function EstimateBuilder({
  estimateId,
  initialLines,
  onSaved,
}: {
  estimateId: string;
  initialLines: ExistingLineItem[];
  onSaved: (total: string, breakdown: CategorySubtotal[]) => void;
}) {
  const { accessToken } = useAuth();
  const [lines, setLines] = React.useState<DraftLine[]>(
    initialLines.map((li) => ({
      cost_catalog_item_id: li.cost_catalog_item_id,
      // Name/unit aren't in the persisted line item shape (only the
      // snapshot rate is) — resolved lazily as "—" until the user re-adds
      // via the catalog panel, or left blank; a full re-hydration would
      // need a catalog lookup by id, which the initial builder pass
      // doesn't do. Acceptable: a draft estimate that already has lines
      // still shows quantity/rate/total correctly, just without a
      // re-derived name label. If this reads poorly in practice during
      // manual verification, extend this constructor to look up names
      // from a fetched catalog map before setting initial state.
      name: "—",
      unit: "",
      unit_rate: li.unit_rate_snapshot,
      quantity: li.quantity,
    }))
  );
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  function handleAdd(item: { id: string; name: string; unit: string; unit_rate: string }) {
    setLines((prev) => {
      if (prev.some((l) => l.cost_catalog_item_id === item.id)) return prev;
      return [
        ...prev,
        { cost_catalog_item_id: item.id, name: item.name, unit: item.unit, unit_rate: item.unit_rate, quantity: "1" },
      ];
    });
  }

  function handleQuantityChange(id: string, quantity: string) {
    setLines((prev) => prev.map((l) => (l.cost_catalog_item_id === id ? { ...l, quantity } : l)));
  }

  function handleRemove(id: string) {
    setLines((prev) => prev.filter((l) => l.cost_catalog_item_id !== id));
  }

  async function handleSave() {
    if (saving || !accessToken) return;
    setError(null);
    setSaving(true);
    try {
      const linesResponse = await fetch(`/api/estimates/${estimateId}/lines`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          items: lines.map((l) => ({ cost_catalog_item_id: l.cost_catalog_item_id, quantity: l.quantity })),
        }),
      });
      const linesData = await linesResponse.json();
      if (!linesResponse.ok) {
        setError(linesData.detail ?? "Failed to save line items");
        return;
      }

      const calcResponse = await fetch(`/api/estimates/${estimateId}/calculate`, {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const calcData = await calcResponse.json();
      if (!calcResponse.ok) {
        setError(calcData.detail ?? "Failed to calculate estimate");
        return;
      }
      onSaved(calcData.total, calcData.category_breakdown);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
      <CatalogPanel onAdd={handleAdd} />
      <div className="flex flex-col gap-3">
        <LineRows lines={lines} onQuantityChange={handleQuantityChange} onRemove={handleRemove} />
        {error && (
          <p role="alert" aria-live="assertive" className="text-sm text-red-600">
            {error}
          </p>
        )}
        <Button type="button" onClick={handleSave} disabled={saving}>
          {saving ? "Saving…" : "Save & calculate"}
        </Button>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Type-check**

```bash
cd frontend
npx tsc --noEmit
```
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/components/estimates/CatalogPanel.tsx frontend/components/estimates/LineRows.tsx frontend/components/estimates/EstimateBuilder.tsx
git commit -m "feat: estimate builder - catalog panel, line rows, save+calculate"
```

## Task 18: Estimate detail page — state machine, PDF panel, duplication

**Files:**
- Create: `frontend/components/estimates/PdfPanel.tsx`
- Create: `frontend/app/(app)/estimates/[id]/page.tsx`

- [ ] **Step 1: `PdfPanel.tsx`**

```tsx
"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";

export function PdfPanel({ estimateId, pdfStatus, canExport }: { estimateId: string; pdfStatus: string; canExport: boolean }) {
  const { accessToken } = useAuth();
  const [status, setStatus] = React.useState(pdfStatus);
  const [viewerUrl, setViewerUrl] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [requesting, setRequesting] = React.useState(false);

  React.useEffect(() => {
    setStatus(pdfStatus);
  }, [pdfStatus]);

  React.useEffect(() => {
    if (status !== "pending" || !accessToken) return;
    const interval = setInterval(async () => {
      const response = await fetch(`/api/estimates/${estimateId}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (response.ok) setStatus(data.pdf_status);
    }, 3000);
    return () => clearInterval(interval);
  }, [status, accessToken, estimateId]);

  const loadViewer = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const response = await fetch(`/api/estimates/${estimateId}/pdf`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!response.ok) return;
      const blob = await response.blob();
      setViewerUrl(URL.createObjectURL(blob));
    } catch {
      // Viewer stays unset — the Download button below still works via its
      // own fetch and is the fallback path if inline preview fails.
    }
  }, [accessToken, estimateId]);

  React.useEffect(() => {
    if (status === "ready") void Promise.resolve().then(() => loadViewer());
  }, [status, loadViewer]);

  React.useEffect(() => {
    return () => {
      if (viewerUrl) URL.revokeObjectURL(viewerUrl);
    };
  }, [viewerUrl]);

  async function handleExport() {
    if (requesting || !accessToken) return;
    setError(null);
    setRequesting(true);
    try {
      const response = await fetch(`/api/estimates/${estimateId}/export`, {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to export PDF");
        return;
      }
      setStatus(data.pdf_status);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setRequesting(false);
    }
  }

  async function handleDownload() {
    if (!accessToken) return;
    try {
      const response = await fetch(`/api/estimates/${estimateId}/pdf`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!response.ok) {
        setError("Download failed");
        return;
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `estimate-${estimateId}.pdf`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }

  return (
    <div className="flex flex-col gap-3 border border-slate-200 rounded-md p-4">
      <p className="text-sm font-medium">PDF export</p>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {status === "not_requested" && canExport && (
        <Button type="button" onClick={handleExport} disabled={requesting}>
          Generate PDF
        </Button>
      )}
      {status === "pending" && <p className="text-sm text-slate-500">Generating… this can take a moment.</p>}
      {status === "failed" && (
        <>
          <p className="text-sm text-red-600">PDF generation failed.</p>
          {canExport && (
            <Button type="button" onClick={handleExport} disabled={requesting}>
              Retry export
            </Button>
          )}
        </>
      )}
      {status === "ready" && (
        <div className="flex flex-col gap-2">
          {viewerUrl && (
            <iframe src={viewerUrl} title="Estimate PDF" className="w-full h-96 border border-slate-200 rounded" />
          )}
          <div className="flex gap-2">
            <Button type="button" variant="outline" onClick={handleDownload}>
              Download
            </Button>
            {canExport && (
              <Button type="button" variant="outline" onClick={handleExport} disabled={requesting}>
                Regenerate
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: `estimates/[id]/page.tsx`**

```tsx
"use client";

import * as React from "react";
import { useParams, useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { StatusBadge } from "@/components/ui/status-badge";
import { EstimateBuilder } from "@/components/estimates/EstimateBuilder";
import { PdfPanel } from "@/components/estimates/PdfPanel";
import { SigningPanel } from "@/components/esign/SigningPanel";
import { formatCurrency } from "@/lib/format";

interface LineItem {
  id: string;
  cost_catalog_item_id: string;
  quantity: string;
  unit_rate_snapshot: string;
  line_total: string;
}

interface CategorySubtotal {
  category: string;
  subtotal: string;
}

interface Estimate {
  id: string;
  status: string;
  pdf_status: string;
  total: string | null;
  markup_profile_id: string;
  esignature_id: string | null;
  line_items: LineItem[];
}

interface MarkupProfileOption {
  id: string;
  name: string;
}

interface Esignature {
  signer_name: string;
  signer_email: string;
  signed_at: string;
  ip_address: string;
}

export default function EstimateDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { accessToken, role } = useAuth();
  const [estimate, setEstimate] = React.useState<Estimate | null>(null);
  const [breakdown, setBreakdown] = React.useState<CategorySubtotal[]>([]);
  const [profiles, setProfiles] = React.useState<MarkupProfileOption[]>([]);
  const [esignature, setEsignature] = React.useState<Esignature | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [duplicating, setDuplicating] = React.useState(false);

  const canEdit = role === "admin" || role === "project_manager";

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const response = await fetch(`/api/estimates/${id}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to load estimate");
        return;
      }
      setEstimate(data);
      if (data.esignature_id) {
        const esigResponse = await fetch(`/api/esignatures/${data.esignature_id}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        if (esigResponse.ok) setEsignature(await esigResponse.json());
      }
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, id]);

  const loadProfiles = React.useCallback(async () => {
    if (!accessToken) return;
    const response = await fetch("/api/markup-profiles", {
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    if (response.ok) setProfiles((await response.json()).items);
  }, [accessToken]);

  React.useEffect(() => {
    void Promise.resolve().then(() => {
      void load();
      void loadProfiles();
    });
  }, [load, loadProfiles]);

  async function handleMarkupChange(markupProfileId: string) {
    if (!accessToken || !estimate) return;
    const response = await fetch(`/api/estimates/${estimate.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
      body: JSON.stringify({ markup_profile_id: markupProfileId }),
    });
    if (response.ok) void load();
  }

  async function handleDelete() {
    if (!accessToken || !estimate) return;
    const response = await fetch(`/api/estimates/${estimate.id}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    if (response.status === 204) router.push("/estimates");
  }

  async function handleSendForSignature() {
    if (!accessToken || !estimate) return;
    const response = await fetch(`/api/estimates/${estimate.id}/send-for-signature`, {
      method: "POST",
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    const data = await response.json();
    if (!response.ok) {
      setError(data.detail ?? "Failed to send for signature");
      return;
    }
    void load();
  }

  async function handleDuplicate() {
    if (!accessToken || !estimate || duplicating) return;
    setDuplicating(true);
    setError(null);
    try {
      const createResponse = await fetch("/api/estimates", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          markup_profile_id: estimate.markup_profile_id,
          // project_id/lead_id: this page doesn't currently fetch the
          // parent binding onto `estimate` (EstimateResponse has both
          // fields but this component's local Estimate interface omits
          // them) — add project_id/lead_id to the Estimate interface above
          // and thread them through here before wiring this button up for
          // real; omitted from this plan step's code sample for brevity,
          // required for a correct implementation.
        }),
      });
      const created = await createResponse.json();
      if (!createResponse.ok) {
        setError(created.detail ?? "Failed to duplicate estimate");
        return;
      }
      await fetch(`/api/estimates/${created.id}/lines`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          items: estimate.line_items.map((li) => ({
            cost_catalog_item_id: li.cost_catalog_item_id,
            quantity: li.quantity,
          })),
        }),
      });
      router.push(`/estimates/${created.id}`);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setDuplicating(false);
    }
  }

  if (!estimate) {
    return (
      <main className="p-6">
        {error ? <p role="alert" className="text-sm text-red-600">{error}</p> : <p className="text-sm text-slate-500">Loading…</p>}
      </main>
    );
  }

  return (
    <main className="p-6 flex flex-col gap-5 max-w-3xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Estimate</h1>
        <StatusBadge status={estimate.status} />
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}

      {estimate.status === "draft" && (
        <>
          {canEdit && (
            <div className="flex items-center gap-2">
              <Select
                aria-label="Markup profile"
                className="w-56"
                value={estimate.markup_profile_id}
                onChange={(e) => handleMarkupChange(e.target.value)}
              >
                {profiles.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </Select>
              <Button type="button" variant="outline" onClick={handleDelete}>
                Delete
              </Button>
              <Button
                type="button"
                onClick={handleSendForSignature}
                disabled={estimate.total === null}
                title={estimate.total === null ? "Save & calculate before sending" : undefined}
              >
                Send for signature
              </Button>
            </div>
          )}
          <EstimateBuilder
            estimateId={estimate.id}
            initialLines={estimate.line_items}
            onSaved={(total, categoryBreakdown) => {
              setEstimate((prev) => (prev ? { ...prev, total } : prev));
              setBreakdown(categoryBreakdown);
            }}
          />
        </>
      )}

      {estimate.status !== "draft" && (
        <div className="flex flex-col gap-4">
          <p className="text-lg font-semibold">{formatCurrency(estimate.total)}</p>
          <ul className="flex flex-col gap-1 text-sm">
            {estimate.line_items.map((li) => (
              <li key={li.id} className="flex justify-between">
                <span>Qty {li.quantity} @ {formatCurrency(li.unit_rate_snapshot)}</span>
                <span>{formatCurrency(li.line_total)}</span>
              </li>
            ))}
          </ul>
          {breakdown.length > 0 && (
            <div className="text-sm text-slate-600">
              {breakdown.map((b) => (
                <div key={b.category} className="flex justify-between">
                  <span>{b.category}</span>
                  <span>{formatCurrency(b.subtotal)}</span>
                </div>
              ))}
            </div>
          )}

          <PdfPanel estimateId={estimate.id} pdfStatus={estimate.pdf_status} canExport={canEdit} />

          {estimate.status === "sent" && role === "client" && accessToken && (
            <SigningPanel
              approveUrl={`/api/estimates/${estimate.id}/approve`}
              rejectUrl={`/api/estimates/${estimate.id}/reject`}
              accessToken={accessToken}
              onDone={load}
            />
          )}
          {estimate.status === "sent" && role !== "client" && (
            <p className="text-sm text-slate-500">Waiting for the client's signature.</p>
          )}

          {estimate.status === "approved" && esignature && (
            <div className="text-sm border border-slate-200 rounded-md p-3">
              <p className="font-medium">Signed</p>
              <p>{esignature.signer_name} ({esignature.signer_email})</p>
              <p className="text-slate-500">
                {new Date(esignature.signed_at).toLocaleString()} · {esignature.ip_address}
              </p>
            </div>
          )}

          {estimate.status === "rejected" && (
            <p className="text-sm text-red-600">This estimate was rejected by the client.</p>
          )}

          {canEdit && (estimate.status === "approved" || estimate.status === "rejected") && (
            <Button type="button" variant="outline" onClick={handleDuplicate} disabled={duplicating}>
              {duplicating ? "Duplicating…" : "Duplicate as new draft"}
            </Button>
          )}
        </div>
      )}
    </main>
  );
}
```

Before finalizing this step, resolve the `handleDuplicate` gap flagged in its own inline comment: add `project_id: string | null` and `lead_id: string | null` to the `Estimate` interface at the top of this file (both already exist on the backend's `EstimateResponse`/`EstimateDetailResponse`, so no type-generation gap), and pass `project_id: estimate.project_id, lead_id: estimate.lead_id` in the create-request body instead of omitting them. This is required for `handleDuplicate` to work correctly — the plan's own code sample above deliberately leaves it as a flagged gap rather than silently shipping a broken duplicate that creates a parent-less estimate.

- [ ] **Step 3: Type-check**

```bash
cd frontend
npx tsc --noEmit
```
Expected: exit 0.

- [ ] **Step 4: Commit**

```bash
git add frontend/components/estimates/PdfPanel.tsx "frontend/app/(app)/estimates/[id]"
git commit -m "feat: estimate detail - draft builder, sent/approved/rejected states, PDF panel, duplication"
```

## Task 19: Catalog page — cost items + markup profiles tabs

**Files:**
- Create: `frontend/components/catalog/CatalogItemsTab.tsx`
- Create: `frontend/components/catalog/MarkupProfilesTab.tsx`
- Create: `frontend/app/(app)/catalog/page.tsx`

- [ ] **Step 1: `CatalogItemsTab.tsx`**

```tsx
"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { formatCurrency } from "@/lib/format";

interface CatalogItem {
  id: string;
  category: string;
  name: string;
  unit: string;
  unit_rate: string;
  is_override: boolean;
}

export function CatalogItemsTab() {
  const { accessToken, role } = useAuth();
  const [items, setItems] = React.useState<CatalogItem[]>([]);
  const [editingId, setEditingId] = React.useState<string | null>(null);
  const [editRate, setEditRate] = React.useState("");
  const [category, setCategory] = React.useState("");
  const [name, setName] = React.useState("");
  const [unit, setUnit] = React.useState("");
  const [unitRate, setUnitRate] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const canWrite = role === "admin" || role === "project_manager";

  const loadAll = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const all: CatalogItem[] = [];
      let cursor: string | null = null;
      do {
        const params = new URLSearchParams();
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/catalog/items?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (!response.ok) {
          setError(data.detail ?? "Failed to load catalog items");
          return;
        }
        all.push(...data.items);
        cursor = data.next_cursor ?? null;
      } while (cursor);
      setItems(all);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken]);

  React.useEffect(() => {
    void Promise.resolve().then(() => loadAll());
  }, [loadAll]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch("/api/catalog/items", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ category, name, unit, unit_rate: unitRate }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to create catalog item");
        return;
      }
      setCategory("");
      setName("");
      setUnit("");
      setUnitRate("");
      await loadAll();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleSaveRate(itemId: string) {
    if (!accessToken) return;
    const response = await fetch(`/api/catalog/items/${itemId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
      body: JSON.stringify({ unit_rate: editRate }),
    });
    if (response.ok) {
      setEditingId(null);
      await loadAll();
    }
  }

  async function handleDelete(itemId: string) {
    if (!accessToken) return;
    const response = await fetch(`/api/catalog/items/${itemId}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    if (response.status === 204) {
      await loadAll();
    } else {
      const data = await response.json();
      setError(data.detail ?? "Failed to delete catalog item");
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {canWrite && (
        <form onSubmit={handleCreate} className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="cat-category">Category</Label>
            <Input id="cat-category" value={category} onChange={(e) => setCategory(e.target.value)} disabled={submitting} required />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="cat-name">Name</Label>
            <Input id="cat-name" value={name} onChange={(e) => setName(e.target.value)} disabled={submitting} required />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="cat-unit">Unit</Label>
            <Input id="cat-unit" className="w-20" value={unit} onChange={(e) => setUnit(e.target.value)} disabled={submitting} required />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="cat-rate">Unit rate</Label>
            <Input id="cat-rate" className="w-28" type="number" step="0.01" value={unitRate} onChange={(e) => setUnitRate(e.target.value)} disabled={submitting} required />
          </div>
          <Button type="submit" disabled={submitting}>Add item</Button>
        </form>
      )}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {items.map((item) => (
          <li key={item.id} className="flex items-center gap-3 px-4 py-2 text-sm">
            <span className="w-32 text-slate-500">{item.category}</span>
            <span className="flex-1">{item.name}{item.is_override && <span className="ml-2 text-xs text-blue-600">override</span>}</span>
            {editingId === item.id ? (
              <>
                <Input className="w-24 h-8" value={editRate} onChange={(e) => setEditRate(e.target.value)} />
                <Button type="button" size="sm" onClick={() => handleSaveRate(item.id)}>Save</Button>
              </>
            ) : (
              <span>{formatCurrency(item.unit_rate)}/{item.unit}</span>
            )}
            {canWrite && editingId !== item.id && (
              <>
                <button type="button" onClick={() => { setEditingId(item.id); setEditRate(item.unit_rate); }} className="text-slate-400 hover:text-slate-700">Edit</button>
                <button type="button" onClick={() => handleDelete(item.id)} className="text-slate-400 hover:text-red-600">Delete</button>
              </>
            )}
          </li>
        ))}
        {items.length === 0 && <li className="px-4 py-3 text-sm text-slate-500">No catalog items yet.</li>}
      </ul>
    </div>
  );
}
```

- [ ] **Step 2: `MarkupProfilesTab.tsx`**

```tsx
"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface MarkupProfile {
  id: string;
  name: string;
  overhead_pct: string;
  profit_pct: string;
}

export function MarkupProfilesTab() {
  const { accessToken, role } = useAuth();
  const [profiles, setProfiles] = React.useState<MarkupProfile[]>([]);
  const [name, setName] = React.useState("");
  const [overheadPct, setOverheadPct] = React.useState("0");
  const [profitPct, setProfitPct] = React.useState("0");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const canWrite = role === "admin" || role === "project_manager";

  const loadAll = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const all: MarkupProfile[] = [];
      let cursor: string | null = null;
      do {
        const params = new URLSearchParams();
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/markup-profiles?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (!response.ok) {
          setError(data.detail ?? "Failed to load markup profiles");
          return;
        }
        all.push(...data.items);
        cursor = data.next_cursor ?? null;
      } while (cursor);
      setProfiles(all);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken]);

  React.useEffect(() => {
    void Promise.resolve().then(() => loadAll());
  }, [loadAll]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch("/api/markup-profiles", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ name, overhead_pct: overheadPct, profit_pct: profitPct }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to create markup profile");
        return;
      }
      setName("");
      setOverheadPct("0");
      setProfitPct("0");
      await loadAll();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDelete(profileId: string) {
    if (!accessToken) return;
    const response = await fetch(`/api/markup-profiles/${profileId}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    if (response.status === 204) {
      await loadAll();
    } else {
      const data = await response.json();
      setError(data.detail ?? "Failed to delete markup profile");
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {canWrite && (
        <form onSubmit={handleCreate} className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="markup-name">Name</Label>
            <Input id="markup-name" value={name} onChange={(e) => setName(e.target.value)} disabled={submitting} required />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="markup-overhead">Overhead %</Label>
            <Input id="markup-overhead" className="w-24" type="number" step="0.01" value={overheadPct} onChange={(e) => setOverheadPct(e.target.value)} disabled={submitting} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="markup-profit">Profit %</Label>
            <Input id="markup-profit" className="w-24" type="number" step="0.01" value={profitPct} onChange={(e) => setProfitPct(e.target.value)} disabled={submitting} />
          </div>
          <Button type="submit" disabled={submitting}>Add profile</Button>
        </form>
      )}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {profiles.map((profile) => (
          <li key={profile.id} className="flex items-center gap-3 px-4 py-2 text-sm">
            <span className="flex-1">{profile.name}</span>
            <span className="text-slate-500">{profile.overhead_pct}% overhead · {profile.profit_pct}% profit</span>
            {canWrite && (
              <button type="button" onClick={() => handleDelete(profile.id)} className="text-slate-400 hover:text-red-600">Delete</button>
            )}
          </li>
        ))}
        {profiles.length === 0 && <li className="px-4 py-3 text-sm text-slate-500">No markup profiles yet.</li>}
      </ul>
    </div>
  );
}
```

- [ ] **Step 3: `catalog/page.tsx`**

```tsx
"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { cn } from "@/lib/utils";
import { CatalogItemsTab } from "@/components/catalog/CatalogItemsTab";
import { MarkupProfilesTab } from "@/components/catalog/MarkupProfilesTab";

const TABS = ["Cost items", "Markup profiles", "PDF template"] as const;
type Tab = (typeof TABS)[number];

export default function CatalogPage() {
  const { role } = useAuth();
  const [tab, setTab] = React.useState<Tab>("Cost items");
  const visibleTabs = role === "admin" ? TABS : TABS.filter((t) => t !== "PDF template");

  return (
    <main className="p-6 flex flex-col gap-5 max-w-3xl">
      <h1 className="text-xl font-semibold">Catalog</h1>
      <div className="flex gap-1 border-b border-slate-200" role="tablist">
        {visibleTabs.map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            onClick={() => setTab(t)}
            className={cn(
              "px-3 py-2 text-sm",
              tab === t ? "border-b-2 border-blue-600 font-medium text-slate-900" : "text-slate-600 hover:text-slate-900"
            )}
          >
            {t}
          </button>
        ))}
      </div>
      {tab === "Cost items" && <CatalogItemsTab />}
      {tab === "Markup profiles" && <MarkupProfilesTab />}
      {tab === "PDF template" && role === "admin" && <PdfTemplatePlaceholder />}
    </main>
  );
}

// Replaced by BrandingTab in Task 20 — left as an inline placeholder here
// so this task's own tsc/lint/build checks pass in isolation before that
// task wires in the real component.
function PdfTemplatePlaceholder() {
  return <p className="text-sm text-slate-500">Loading…</p>;
}
```

- [ ] **Step 4: Type-check**

```bash
cd frontend
npx tsc --noEmit
```
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/components/catalog/CatalogItemsTab.tsx frontend/components/catalog/MarkupProfilesTab.tsx "frontend/app/(app)/catalog"
git commit -m "feat: catalog page - cost items and markup profiles tabs"
```

## Task 20: CSV import/export + PDF template (branding) tab

**Files:**
- Create: `frontend/components/catalog/CsvImport.tsx`
- Create: `frontend/components/catalog/BrandingTab.tsx`
- Modify: `frontend/components/catalog/CatalogItemsTab.tsx`
- Modify: `frontend/app/(app)/catalog/page.tsx`

- [ ] **Step 1: `CsvImport.tsx`**

```tsx
"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { parseCatalogCsv, serializeCatalogCsv, CsvParseError, CatalogCsvRow } from "@/lib/csv";

interface ImportResultEntry {
  index: number;
  status: string;
  detail: string | null;
}

export function CsvImport({
  currentItems,
  onImported,
}: {
  currentItems: CatalogCsvRow[];
  onImported: () => void;
}) {
  const { accessToken } = useAuth();
  const fileInputRef = React.useRef<HTMLInputElement | null>(null);
  const [preview, setPreview] = React.useState<CatalogCsvRow[] | null>(null);
  const [parseError, setParseError] = React.useState<string | null>(null);
  const [results, setResults] = React.useState<ImportResultEntry[] | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  async function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setParseError(null);
    setResults(null);
    try {
      const text = await file.text();
      const rows = parseCatalogCsv(text);
      setPreview(rows);
    } catch (err) {
      setPreview(null);
      setParseError(err instanceof CsvParseError ? err.message : "Unable to read file");
    }
  }

  async function handleImport() {
    if (!preview || submitting || !accessToken) return;
    setSubmitting(true);
    try {
      const response = await fetch("/api/catalog/items/bulk", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          items: preview.map((r) => ({
            category: r.category,
            name: r.name,
            unit: r.unit,
            unit_rate: r.unit_rate,
          })),
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        setParseError(data.detail ?? "Import failed");
        return;
      }
      setResults(data.results);
      setPreview(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      onImported();
    } catch {
      setParseError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  function handleExport() {
    const csv = serializeCatalogCsv(currentItems);
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "catalog-export.csv";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  return (
    <div className="flex flex-col gap-2 border border-slate-200 rounded-md p-3">
      <div className="flex items-center gap-2">
        <input
          ref={fileInputRef}
          type="file"
          accept=".csv,text/csv"
          aria-label="Import CSV"
          onChange={handleFileChange}
          className="text-sm"
        />
        <Button type="button" variant="outline" size="sm" onClick={handleExport}>
          Export CSV
        </Button>
      </div>
      {parseError && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {parseError}
        </p>
      )}
      {preview && (
        <div className="flex flex-col gap-2">
          <p className="text-sm">{preview.length} row(s) ready to import.</p>
          <Button type="button" size="sm" onClick={handleImport} disabled={submitting}>
            {submitting ? "Importing…" : "Import"}
          </Button>
        </div>
      )}
      {results && (
        <ul className="text-sm">
          {results.map((r) => (
            <li key={r.index} className={r.status === "error" ? "text-red-600" : "text-green-700"}>
              Row {r.index + 1}: {r.status}
              {r.detail ? ` — ${r.detail}` : ""}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
```

`CsvImport` does not enforce a 500-row cap client-side beyond what the parser naturally handles — the backend's `Field(..., max_length=500)` on `CostCatalogItemBulkCreateRequest` already rejects an oversized batch with a 422, and that 422's detail surfaces through `parseError` exactly like any other failure. Add an explicit pre-submit row-count check only if manual verification in Task 22 shows the generic 422 message reads poorly to a user who just uploaded a 600-row file — not required for this task's own completion.

- [ ] **Step 2: Wire `CsvImport` into `CatalogItemsTab.tsx`**

In `frontend/components/catalog/CatalogItemsTab.tsx`, add the import `import { CsvImport } from "./CsvImport";` and render it inside the `canWrite` block, after the create form:

```tsx
      {canWrite && (
        <CsvImport
          currentItems={items.map((i) => ({ category: i.category, name: i.name, unit: i.unit, unit_rate: i.unit_rate }))}
          onImported={loadAll}
        />
      )}
```

- [ ] **Step 3: `BrandingTab.tsx`**

```tsx
"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

interface Branding {
  logo_storage_path: string | null;
  accent_color: string;
  footer_text: string;
}

export function BrandingTab() {
  const { accessToken } = useAuth();
  const [branding, setBranding] = React.useState<Branding | null>(null);
  const [accentColor, setAccentColor] = React.useState("#1e293b");
  const [footerText, setFooterText] = React.useState("");
  const [logoFile, setLogoFile] = React.useState<File | null>(null);
  const [submitting, setSubmitting] = React.useState(false);
  const [saved, setSaved] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const response = await fetch("/api/companies/branding", {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to load branding");
        return;
      }
      setBranding(data);
      setAccentColor(data.accent_color);
      setFooterText(data.footer_text);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken]);

  React.useEffect(() => {
    void Promise.resolve().then(() => load());
  }, [load]);

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSaved(false);
    setSubmitting(true);
    try {
      const response = await fetch("/api/companies/branding", {
        method: "PUT",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ accent_color: accentColor, footer_text: footerText }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to save branding");
        return;
      }

      if (logoFile) {
        const formData = new FormData();
        formData.append("file", logoFile);
        const logoResponse = await fetch("/api/companies/branding/logo", {
          method: "POST",
          headers: { Authorization: `Bearer ${accessToken}` },
          body: formData,
        });
        const logoData = await logoResponse.json();
        if (!logoResponse.ok) {
          setError(logoData.detail ?? "Failed to upload logo");
          return;
        }
      }

      setSaved(true);
      await load();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  if (!branding) {
    return <p className="text-sm text-slate-500">Loading…</p>;
  }

  return (
    <form onSubmit={handleSave} className="flex flex-col gap-4 max-w-md">
      <p className="text-sm text-slate-500">Applies to future PDF exports — already-generated PDFs don't change.</p>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="branding-logo">Logo (PNG or JPEG, up to 2 MB)</Label>
        {branding.logo_storage_path && <p className="text-xs text-slate-500">Current logo is set.</p>}
        <input
          id="branding-logo"
          type="file"
          accept="image/png,image/jpeg"
          onChange={(e) => setLogoFile(e.target.files?.[0] ?? null)}
          disabled={submitting}
          className="text-sm"
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="branding-accent">Accent color</Label>
        <Input
          id="branding-accent"
          type="text"
          pattern="^#[0-9a-fA-F]{6}$"
          value={accentColor}
          onChange={(e) => setAccentColor(e.target.value)}
          disabled={submitting}
          required
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="branding-footer">Footer / terms text</Label>
        <Textarea
          id="branding-footer"
          value={footerText}
          onChange={(e) => setFooterText(e.target.value)}
          disabled={submitting}
        />
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {saved && <p className="text-sm text-green-700">Saved.</p>}
      <Button type="submit" disabled={submitting}>
        {submitting ? "Saving…" : "Save"}
      </Button>
    </form>
  );
}
```

- [ ] **Step 4: Wire into `catalog/page.tsx`**

In `frontend/app/(app)/catalog/page.tsx`, replace `import { CsvImport... }`-adjacent `PdfTemplatePlaceholder` usage: add `import { BrandingTab } from "@/components/catalog/BrandingTab";`, remove the `PdfTemplatePlaceholder` function entirely, and change the render line to:

```tsx
      {tab === "PDF template" && role === "admin" && <BrandingTab />}
```

- [ ] **Step 5: Type-check**

```bash
cd frontend
npx tsc --noEmit
```
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add frontend/components/catalog/CsvImport.tsx frontend/components/catalog/BrandingTab.tsx frontend/components/catalog/CatalogItemsTab.tsx "frontend/app/(app)/catalog/page.tsx"
git commit -m "feat: catalog CSV import/export and PDF branding tab"
```

## Task 21: Change orders tab on project detail + Estimates section on lead detail

**Files:**
- Create: `frontend/components/change-orders/ChangeOrdersTab.tsx`
- Modify: `frontend/app/(app)/projects/[id]/page.tsx`
- Modify: `frontend/app/(app)/leads/[id]/page.tsx`

- [ ] **Step 1: `ChangeOrdersTab.tsx`**

```tsx
"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatCurrency } from "@/lib/format";

interface ChangeOrder {
  id: string;
  description: string;
  cost_delta: string;
  schedule_impact_days: number;
  status: string;
}

export function ChangeOrdersTab({ projectId }: { projectId: string }) {
  const { accessToken, role } = useAuth();
  const [changeOrders, setChangeOrders] = React.useState<ChangeOrder[]>([]);
  const [description, setDescription] = React.useState("");
  const [costDelta, setCostDelta] = React.useState("");
  const [scheduleImpactDays, setScheduleImpactDays] = React.useState("0");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const canWrite = role === "admin" || role === "project_manager";

  const loadAll = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const all: ChangeOrder[] = [];
      let cursor: string | null = null;
      do {
        const params = new URLSearchParams();
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/projects/${projectId}/change-orders?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (!response.ok) {
          setError(data.detail ?? "Failed to load change orders");
          return;
        }
        all.push(...data.items);
        cursor = data.next_cursor ?? null;
      } while (cursor);
      setChangeOrders(all);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, projectId]);

  React.useEffect(() => {
    void Promise.resolve().then(() => loadAll());
  }, [loadAll]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch(`/api/projects/${projectId}/change-orders`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          description,
          cost_delta: costDelta,
          schedule_impact_days: Number(scheduleImpactDays) || 0,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to create change order");
        return;
      }
      setDescription("");
      setCostDelta("");
      setScheduleImpactDays("0");
      await loadAll();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleSendForSignature(id: string) {
    if (!accessToken) return;
    const response = await fetch(`/api/change-orders/${id}/send-for-signature`, {
      method: "POST",
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    if (response.ok) await loadAll();
  }

  return (
    <div className="flex flex-col gap-4">
      {canWrite && (
        <form onSubmit={handleCreate} className="flex flex-col gap-3 max-w-md">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="co-description">Description</Label>
            <Textarea id="co-description" value={description} onChange={(e) => setDescription(e.target.value)} disabled={submitting} required />
          </div>
          <div className="flex gap-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="co-cost">Cost delta</Label>
              <Input id="co-cost" type="number" step="0.01" value={costDelta} onChange={(e) => setCostDelta(e.target.value)} disabled={submitting} required />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="co-days">Schedule impact (days)</Label>
              <Input id="co-days" type="number" value={scheduleImpactDays} onChange={(e) => setScheduleImpactDays(e.target.value)} disabled={submitting} />
            </div>
          </div>
          <Button type="submit" disabled={submitting} className="self-start">
            Add change order
          </Button>
        </form>
      )}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {changeOrders.map((co) => (
          <li key={co.id} className="flex items-center gap-3 px-4 py-3 text-sm">
            <span className="flex-1">{co.description}</span>
            <span className={Number(co.cost_delta) < 0 ? "text-green-700" : "text-slate-700"}>
              {formatCurrency(co.cost_delta)}
            </span>
            <StatusBadge status={co.status} />
            {canWrite && co.status === "pending" && (
              <Button type="button" size="sm" variant="outline" onClick={() => handleSendForSignature(co.id)}>
                Send for signature
              </Button>
            )}
          </li>
        ))}
        {changeOrders.length === 0 && <li className="px-4 py-3 text-sm text-slate-500">No change orders yet.</li>}
      </ul>
    </div>
  );
}
```

- [ ] **Step 2: Wire into the project detail page**

Read `frontend/app/(app)/projects/[id]/page.tsx` first (its `TABS` array and tab-render block). Add `"Change orders"` to the `TABS` tuple after `"Daily logs"`, add `import { ChangeOrdersTab } from "@/components/change-orders/ChangeOrdersTab";`, and add the render line: `{tab === "Change orders" && <ChangeOrdersTab projectId={project.id} />}`.

Also add an "Estimates" tab to this same page, following the exact same pattern, rendering a filtered version of the estimates list. Add `"Estimates"` to `TABS`, and add:

```tsx
      {tab === "Estimates" && <ProjectEstimatesTab projectId={project.id} />}
```

with this small inline component added to the same file (below the existing `OverviewTab` function):

```tsx
function ProjectEstimatesTab({ projectId }: { projectId: string }) {
  const { accessToken } = useAuth();
  const [estimates, setEstimates] = React.useState<{ id: string; status: string; total: string | null }[]>([]);

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    const response = await fetch(`/api/estimates`, { headers: { Authorization: `Bearer ${accessToken}` } });
    const data = await response.json();
    if (response.ok) {
      // Client-side filter: no ?project_id= query param exists on
      // GET /estimates (out of this plan's scope to add one) — acceptable
      // for a project with a normal number of estimates; if a company
      // accumulates enough estimates that this filtering matters
      // performance-wise, add server-side project_id filtering as a
      // follow-up, not required for this task.
      setEstimates(data.items.filter((e: { project_id?: string }) => e.project_id === projectId));
    }
  }, [accessToken, projectId]);

  React.useEffect(() => {
    void Promise.resolve().then(() => load());
  }, [load]);

  return (
    <div className="flex flex-col gap-3">
      <Link href={`/estimates/new?project_id=${projectId}`}>
        <Button size="sm">New estimate</Button>
      </Link>
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {estimates.map((e) => (
          <li key={e.id}>
            <Link href={`/estimates/${e.id}`} className="flex items-center justify-between px-4 py-2 text-sm hover:bg-slate-50">
              <StatusBadge status={e.status} />
              <span>{formatCurrency(e.total)}</span>
            </Link>
          </li>
        ))}
        {estimates.length === 0 && <li className="px-4 py-3 text-sm text-slate-500">No estimates yet.</li>}
      </ul>
    </div>
  );
}
```

This uses `Link`, `StatusBadge`, and `formatCurrency` — add the missing imports (`import Link from "next/link";`, `import { StatusBadge } from "@/components/ui/status-badge";` is likely already imported since the page already uses it for the project's own status badge — confirm before adding a duplicate; `formatCurrency` needs adding to the existing `import { formatDate } from "@/lib/format";` line, becoming `import { formatCurrency, formatDate } from "@/lib/format";`).

Note: `GET /estimates` doesn't accept a `project_id` filter (this plan didn't add one — Decision 2's `EstimateListResponse` filter is `status` only). The client-side filter above is a pragmatic accepted limitation for this plan's scope, not a bug to silently work around with a backend change outside this task.

- [ ] **Step 3: Wire an Estimates section into the lead detail page**

Read `frontend/app/(app)/leads/[id]/page.tsx` first. Below the existing communication log section, add a small inline block (same file, no new component needed given its simplicity — reuses the identical filtering pattern from Step 2 but filtered on `lead_id`):

```tsx
      <section className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-medium">Estimates</h2>
          <Link href={`/estimates/new?lead_id=${lead.id}`} className="text-sm text-blue-600 hover:underline">
            New estimate
          </Link>
        </div>
        <LeadEstimatesList leadId={lead.id} />
      </section>
```

placed inside the page's main return, after the communication log's closing tag. Add a `LeadEstimatesList` component in the same file, following `ProjectEstimatesTab`'s exact shape from Step 2 but filtering on `e.lead_id === leadId` instead of `project_id`.

- [ ] **Step 4: Type-check**

```bash
cd frontend
npx tsc --noEmit
```
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/components/change-orders/ChangeOrdersTab.tsx "frontend/app/(app)/projects/[id]/page.tsx" "frontend/app/(app)/leads/[id]/page.tsx"
git commit -m "feat: change orders tab on projects, estimates sections on projects and leads"
```

## Task 22: Client "awaiting your signature" card

**Files:**
- Modify: `frontend/components/projects/ClientProjectDashboard.tsx`

- [ ] **Step 1: Read the existing component**

Read `frontend/components/projects/ClientProjectDashboard.tsx` in full first — this task extends it, not replaces it.

- [ ] **Step 2: Add the awaiting-signature card**

Add a new component in the same file (or a co-located one if the existing file is already large — match whatever this file's current size suggests, following this codebase's "split when a file grows unwieldy" convention noted in the writing-plans skill):

```tsx
function AwaitingSignatureCard({ projectId }: { projectId: string }) {
  const { accessToken } = useAuth();
  const [sentEstimates, setSentEstimates] = React.useState<{ id: string; total: string | null }[]>([]);
  const [pendingChangeOrders, setPendingChangeOrders] = React.useState<
    { id: string; description: string; cost_delta: string }[]
  >([]);
  const [expandedCoId, setExpandedCoId] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    const [estimatesResponse, changeOrdersResponse] = await Promise.all([
      fetch(`/api/estimates?status=sent`, { headers: { Authorization: `Bearer ${accessToken}` } }),
      fetch(`/api/change-orders?status=pending`, { headers: { Authorization: `Bearer ${accessToken}` } }),
    ]);
    if (estimatesResponse.ok) {
      const data = await estimatesResponse.json();
      setSentEstimates(
        data.items.filter((e: { project_id?: string }) => e.project_id === projectId)
      );
    }
    if (changeOrdersResponse.ok) {
      const data = await changeOrdersResponse.json();
      setPendingChangeOrders(
        data.items.filter((co: { project_id?: string }) => co.project_id === projectId)
      );
    }
  }, [accessToken, projectId]);

  React.useEffect(() => {
    void Promise.resolve().then(() => load());
  }, [load]);

  if (sentEstimates.length === 0 && pendingChangeOrders.length === 0) return null;

  return (
    <div className="flex flex-col gap-3 border border-amber-200 bg-amber-50 rounded-md p-4">
      <p className="text-sm font-medium">Awaiting your signature</p>
      {sentEstimates.map((e) => (
        <Link key={e.id} href={`/estimates/${e.id}`} className="text-sm text-blue-600 hover:underline">
          Estimate — {formatCurrency(e.total)}
        </Link>
      ))}
      {pendingChangeOrders.map((co) => (
        <div key={co.id} className="flex flex-col gap-2">
          <button
            type="button"
            onClick={() => setExpandedCoId(expandedCoId === co.id ? null : co.id)}
            className="text-sm text-left text-blue-600 hover:underline"
          >
            Change order — {co.description} ({formatCurrency(co.cost_delta)})
          </button>
          {expandedCoId === co.id && accessToken && (
            <SigningPanel
              approveUrl={`/api/change-orders/${co.id}/approve`}
              rejectUrl={`/api/change-orders/${co.id}/reject`}
              accessToken={accessToken}
              onDone={load}
            />
          )}
        </div>
      ))}
    </div>
  );
}
```

Add imports at the top of the file: `import Link from "next/link";`, `import { useAuth } from "@/contexts/AuthContext";` (only if not already present — the file already receives `project` as a prop and may not currently call `useAuth` itself; check before adding a duplicate), `import { SigningPanel } from "@/components/esign/SigningPanel";`, `import { formatCurrency } from "@/lib/format";` (merge into an existing `@/lib/format` import if one exists).

Render `<AwaitingSignatureCard projectId={project.id} />` inside the existing `ClientProjectDashboard` component's returned JSX, near the top (above the existing status/phase/task summary content — the signature action is the most time-sensitive thing a client needs to see).

- [ ] **Step 3: Type-check**

```bash
cd frontend
npx tsc --noEmit
```
Expected: exit 0.

- [ ] **Step 4: Commit**

```bash
git add frontend/components/projects/ClientProjectDashboard.tsx
git commit -m "feat: client awaiting-signature card for estimates and change orders"
```

## Task 23: Playwright E2E — full estimation + e-signature arc

**Files:**
- Create: `frontend/e2e/estimation.spec.ts`

- [ ] **Step 1: Write the spec**

Read `frontend/e2e/crm-pm.spec.ts` first for the established conventions this spec must match: 15s per-assertion timeouts on first-hit-of-a-route-handler transitions (cold `next dev` compilation), `test.setTimeout` raised for a long multi-step arc, `.example` email domain, `correct-horse-battery-9` password convention, `exact: true` on ambiguous nav link text.

This spec additionally needs a **direct backend API call** (not through the frontend BFF) to seed a client user via the invitation flow, since no invitation-acceptance UI exists. Playwright's `request` fixture (not `page`) does this — it's a separate HTTP client, independent of the browser.

```typescript
import { randomUUID } from "node:crypto";
import { test, expect, request as playwrightRequest } from "@playwright/test";

const BACKEND_URL = process.env.E2E_BACKEND_URL ?? "http://localhost:8000";

test("estimation and e-signature: catalog, builder, PDF, client sign-off, change order", async ({ page }) => {
  test.setTimeout(240_000);

  const suffix = randomUUID().slice(0, 8);
  const adminEmail = `e2e-est-${suffix}@foundation.example`;
  const clientEmail = `e2e-est-client-${suffix}@foundation.example`;
  const password = "correct-horse-battery-9";

  let projectId = "";
  let markupProfileId = "";
  let catalogItemId = "";
  let estimateId = "";
  let adminAccessToken = "";
  let companyId = "";

  await test.step("register admin and land on dashboard", async () => {
    await page.goto("/register");
    await page.getByLabel("Company name").fill(`E2E Estimation Co ${suffix}`);
    await page.getByLabel("Your name").fill("E2E Estimation Tester");
    await page.getByLabel("Email").fill(adminEmail);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
  });

  await test.step("create a catalog item and a markup profile", async () => {
    await page.getByRole("link", { name: "Catalog", exact: true }).click();
    await page.getByLabel("Category").fill("Framing");
    await page.getByLabel("Name", { exact: true }).fill("Lumber");
    await page.getByLabel("Unit").fill("bf");
    await page.getByLabel("Unit rate").fill("4.00");
    await page.getByRole("button", { name: "Add item" }).click();
    await expect(page.getByText("Lumber")).toBeVisible({ timeout: 15_000 });

    await page.getByRole("tab", { name: "Markup profiles" }).click();
    await page.getByLabel("Name", { exact: true }).fill("Standard");
    await page.getByLabel("Overhead %").fill("10");
    await page.getByLabel("Profit %").fill("15");
    await page.getByRole("button", { name: "Add profile" }).click();
    await expect(page.getByText("Standard")).toBeVisible({ timeout: 15_000 });
  });

  await test.step("create a project and an estimate against it", async () => {
    await page.getByRole("link", { name: "Projects", exact: true }).click();
    await page.getByRole("link", { name: "New project" }).click();
    await page.getByLabel("Project name").fill(`Deck ${suffix}`);
    await page.getByLabel("Site address").fill("1 Main St");
    await page.getByRole("button", { name: "Create project" }).click();
    await expect(page.getByRole("heading", { name: `Deck ${suffix}` })).toBeVisible({ timeout: 15_000 });

    await page.getByRole("tab", { name: "Estimates" }).click();
    await page.getByRole("link", { name: "New estimate" }).click();
    await page.getByLabel("Markup profile").selectOption({ label: "Standard" });
    await page.getByRole("button", { name: "Create estimate" }).click();
    await expect(page).toHaveURL(/\/estimates\/[0-9a-f-]+$/, { timeout: 15_000 });
  });

  await test.step("build the estimate and calculate", async () => {
    await page.getByRole("button", { name: "+" }).first().click();
    await page.getByLabel(/Quantity for/).fill("10");
    await page.getByRole("button", { name: "Save & calculate" }).click();
    await expect(page.getByText("Subtotal (before markup)")).toBeVisible({ timeout: 15_000 });
  });

  await test.step("export and download the PDF", async () => {
    await page.getByRole("button", { name: "Generate PDF" }).click();
    await expect(page.getByRole("button", { name: "Download" })).toBeVisible({ timeout: 30_000 });
  });

  await test.step("send for signature", async () => {
    await page.getByRole("button", { name: "Send for signature" }).click();
    await expect(page.getByText("Waiting for the client's signature.")).toBeVisible({ timeout: 15_000 });
    estimateId = page.url().split("/estimates/")[1];
  });

  await test.step("seed a client user via the backend invitation API and sign as them", async () => {
    // Extract the admin's access token and company id from the browser's
    // in-memory AuthContext state via localStorage/sessionStorage is NOT
    // possible (the access token lives only in React state per the BFF
    // session design, never in web storage) — instead, log the admin in
    // again directly against the backend to obtain a fresh token for this
    // API-only client, independent of the browser session.
    const apiContext = await playwrightRequest.newContext({ baseURL: BACKEND_URL });

    const loginResponse = await apiContext.post("/auth/login", {
      data: { email: adminEmail, password },
    });
    expect(loginResponse.ok()).toBeTruthy();
    const loginBody = await loginResponse.json();
    adminAccessToken = loginBody.access_token;

    const invitationResponse = await apiContext.post("/invitations", {
      headers: { Authorization: `Bearer ${adminAccessToken}` },
      data: { email: clientEmail, role: "client" },
    });
    expect(invitationResponse.ok()).toBeTruthy();
    const invitation = await invitationResponse.json();

    const acceptResponse = await apiContext.post(`/invitations/${invitation.id}/accept`, {
      data: { password, full_name: "E2E Client" },
    });
    expect(acceptResponse.ok()).toBeTruthy();

    await apiContext.dispose();

    // Now sign in as the client THROUGH THE BROWSER (a real UI session,
    // not the API context above) — the whole point of this arc is
    // verifying the in-app typed-signature UI actually works.
    await page.getByRole("button", { name: "Log out" }).click();
    await expect(page).toHaveURL(/\/login/);
    await page.getByLabel("Email").fill(clientEmail);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Log in" }).click();
    await expect(page).toHaveURL(/\/(dashboard|projects)/, { timeout: 15_000 });

    await page.goto(`/estimates/${estimateId}`);
    await expect(page.getByLabel("Full name")).toBeVisible({ timeout: 15_000 });
    await page.getByLabel("Full name").fill("E2E Client");
    await page.getByLabel("Email", { exact: true }).fill(clientEmail);
    await page.getByRole("button", { name: "Approve & sign" }).click();
    await expect(page.getByText("Signed")).toBeVisible({ timeout: 15_000 });
  });

  await test.step("admin duplicates the approved estimate", async () => {
    await page.getByRole("button", { name: "Log out" }).click();
    await expect(page).toHaveURL(/\/login/);
    await page.getByLabel("Email").fill(adminEmail);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Log in" }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });

    await page.goto(`/estimates/${estimateId}`);
    await page.getByRole("button", { name: "Duplicate as new draft" }).click();
    await expect(page).toHaveURL(/\/estimates\/[0-9a-f-]+$/, { timeout: 15_000 });
    await expect(page.getByText(/Qty 10/)).toBeVisible({ timeout: 15_000 });
  });

  await test.step("change order blocks completion until approved", async () => {
    await page.getByRole("link", { name: "Projects", exact: true }).click();
    await page.getByRole("link", { name: `Deck ${suffix}` }).click();
    await page.getByRole("button", { name: "Move to pre-construction" }).click();
    await expect(page.getByRole("button", { name: "Move to active" })).toBeVisible({ timeout: 15_000 });
    await page.getByRole("button", { name: "Move to active" }).click();
    await expect(page.getByRole("button", { name: "Move to completed" })).toBeVisible({ timeout: 15_000 });

    await page.getByRole("tab", { name: "Change orders" }).click();
    await page.getByLabel("Description").fill("Add railing");
    await page.getByLabel("Cost delta").fill("250");
    await page.getByRole("button", { name: "Add change order" }).click();
    await expect(page.getByText("Add railing")).toBeVisible({ timeout: 15_000 });

    await page.getByRole("button", { name: "Move to completed" }).click();
    await expect(page.getByText(/pending approval/i)).toBeVisible({ timeout: 15_000 });
  });
});
```

The `page.getByRole("button", { name: "+" }).first()` selector in the "build the estimate" step is fragile if the catalog panel renders more than one `+` button before the intended row (e.g. from a prior test's leftover data on a shared dev server) — if this proves flaky during Step 2's live run, replace it with a more specific selector scoped to the "Lumber" row (e.g. locate the row containing the text "Lumber" first, then find its `+` button within that scope).

- [ ] **Step 2: Run against the live stack**

Requires the worktree's Docker Compose stack up (same as Task 10, Step 1) and the frontend dev server reachable — same environment setup as `crm-pm.spec.ts`'s own established run procedure (cold `.next` cache, `docker compose exec redis redis-cli DEL "ratelimit:register:<container-ip>"` before each run if the register rate limit trips from repeated test registrations, `E2E_BASE_URL=http://localhost:3001` if that's this worktree's published frontend port, `E2E_BACKEND_URL=http://localhost:8000` or whatever port the worktree's backend container publishes — check `docker compose port backend 8000` if unsure).

```bash
cd frontend
$env:E2E_BASE_URL = "http://localhost:3001"
$env:E2E_BACKEND_URL = "http://localhost:8000"
npx playwright test e2e/estimation.spec.ts
```

Expected: `1 passed`. Debug any selector mismatches against the actual rendered DOM (`npx playwright test --debug` or reviewing the failure's `error-context.md` snapshot, same workflow used throughout the CRM+PM plan's own Task 20) — do not weaken an assertion to make it pass; fix the selector or the underlying page if there's a real mismatch.

- [ ] **Step 3: Run the full E2E suite together**

```bash
npx playwright test
```

Expected: all specs (`foundation.spec.ts`, `crm-pm.spec.ts`, `estimation.spec.ts`) pass together — a shared dev server across specs means catalog items/estimates from this task's run must not collide with or corrupt earlier specs' assertions (they run in different companies via distinct registrations, so this should hold by construction; verify it does).

- [ ] **Step 4: Commit**

```bash
git add frontend/e2e/estimation.spec.ts
git commit -m "test: E2E arc - catalog, estimate builder, PDF export, client e-signature, change order"
```

## Task 24: Consolidated review, lint/build, full regression, docs sync, closeout, PR

**Files:** `docs/superpowers/specs/2026-07-20-estimation-esignature-frontend-design.md` (Implementation Status addendum).

- [ ] **Step 1: Consolidated review of the screen layer**

Before the live E2E gate, dispatch one review pass over the entire frontend screen layer added in Tasks 11–22 (`git diff main...HEAD -- frontend/`), following the same pattern the CRM+PM plan used: a single reviewer covering integration seams (does every `fetch("/api/...")` call hit a handler that exists with the right method?), correctness (cursor pagination followed to exhaustion where a "must see the new row" flow needs it — the CSV-imported items, a just-created estimate, a just-added change order), auth-during-hydration guards, and accessibility conventions (`submitting` guards, `aria-live` errors). Fix anything Important or higher before proceeding; note Minor findings but don't block on them.

- [ ] **Step 2: Frontend lint + production build**

```bash
cd frontend
npm run lint
npm run build
```

Expected: both exit 0 (any pre-existing marketing `<img>` warnings are fine; 0 errors). Fix any real errors surfaced by the production compile — never by disabling rules. If `npm run lint` flags `react-hooks/set-state-in-effect` on any of this plan's new `useEffect`/`load` pairs, apply the CRM+PM plan's own established fix (`void Promise.resolve().then(() => load())` deferral, already used throughout Tasks 16–22's code samples above) rather than inventing a new pattern.

- [ ] **Step 3: Full backend regression**

```bash
cd backend
$env:REDIS_URL = "redis://localhost:16379/0"
.venv\Scripts\python.exe -m pytest
```

Expected: full suite green (should be ~800+ tests including this plan's additions from Tasks 1–8). Never run two pytest invocations concurrently — they share the test database.

- [ ] **Step 4: Docs sync**

Add an **Implementation Status** paragraph directly under the title of `docs/superpowers/specs/2026-07-20-estimation-esignature-frontend-design.md` (the established convention — see the CRM+PM spec's own addendum for tone/format): completion statement, lint/build results, live E2E results (all three specs), and any deliberately-deferred items or accepted limitations surfaced during implementation (e.g. the client-side project/lead filtering on the estimates list from Task 21, if it's still in place rather than replaced by a server-side filter; the `handleDuplicate` project_id/lead_id gap from Task 18 once resolved — confirm it's fixed and note that it is).

- [ ] **Step 5: Commit the closeout**

```bash
cd "D:\Development\New const proj mgt software\.worktrees\estimation-esignature"
git add docs/superpowers/specs/2026-07-20-estimation-esignature-frontend-design.md
git commit -m "docs: close out Estimation + E-Signature frontend implementation"
```

- [ ] **Step 6: Push + PR**

```bash
git push -u origin feature/estimation-esignature-frontend
```

Write the PR body to a scratch file first (embedded quotes break shell argument quoting), then:

```bash
gh pr create --base main --head feature/estimation-esignature-frontend --title "feat: Estimation + E-Signature frontend - catalog, estimate builder, PDF export, change orders" --body-file <scratch-file-path>
```

Confirm CI (backend-ci and frontend-ci) goes green. **Merging remains an explicit, separate user decision — not automatic**, matching every prior feature in this project.

---

## Plan self-review notes

**Spec coverage:** Decision 1 (9 backend gaps) → Tasks 1–8. Decision 2 (routes/IA) → Tasks 16, 21. Decision 3 (state-driven detail page) → Task 18. Decision 4 (typed e-sig) → Task 12. Decision 5 (PDF lifecycle) → Task 18 Step 1 (`PdfPanel`). Decision 6 (change orders) → Task 21. Decision 7 (duplication) → Task 18. Decision 8 (branding) → Tasks 8, 20. Decision 9 (CSV) → Tasks 7, 20. Decision 10 (BFF handlers) → Tasks 13–15. Decision 11 (components) → throughout; `DuplicateButton` was folded inline into `estimates/[id]/page.tsx` rather than split into its own file — a reasonable deviation given its small size, noted here rather than silently diverging from the spec's component list. Decision 12 (errors/empty states) → conventions applied throughout. Decision 13 (E2E) → Task 23.

**Known follow-ups flagged inline, not silently glossed over:** Task 4 Step 4 flags a migration check for `ON DELETE CASCADE` on estimate line items that must be verified, not assumed. Task 7 Step 4 flags that the bulk-import test in Step 1 needs reconciling with Pydantic-vs-runtime validation timing — verify against the real implementation before finalizing. Task 18 Step 2 explicitly requires resolving the `handleDuplicate` project_id/lead_id gap before that step is considered done, not leaving the sample code's own flagged placeholder in place.
