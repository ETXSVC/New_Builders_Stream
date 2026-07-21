"""ESTIMATE_APPROVED -> BOM line generation/merge (design spec Decision 4).

A SECOND handler subscribed to the same event `handle_estimate_approved`
(app/services/estimate_approved_handler.py) already subscribes to —
app.core.events.register() supports multiple handlers per event name,
called in registration order (see that module's own docstring). This is
a distinct concern (materials tracking vs. deposit invoicing) from the
same trigger, so it lives in its own file rather than being folded into
the existing handler, matching this codebase's precedent of one handler
per concern even when they share a trigger event.

Inherited Invariant #4: reuses the caller's session and MUST NEVER call
session.commit()/rollback() itself — only flush().
"""
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tier_gating import tier_allows
from app.models import BomLine, CostCatalogItem, EstimateLineItem
from app.services.audit import write_audit_log


async def handle_estimate_approved_bom(
    *,
    session: AsyncSession,
    estimate_id: uuid.UUID,
    project_id: uuid.UUID | None,
    company_id: uuid.UUID,
    **_ignored: object,
) -> None:
    # Same no-op-on-bare-lead reasoning as handle_estimate_approved.
    if project_id is None:
        return

    # Same tier-gating reasoning as handle_estimate_approved's own
    # accounting-tier check: this is an ESTIMATION-module write reached
    # through the event bus, not through that module's own routes —
    # without this check a starter-tier company would get BomLines
    # auto-created into a module its plan doesn't include.
    if not await tier_allows(session, company_id, "estimation"):
        return

    # EstimateLineItem.cost_catalog_item_id is NOT NULL (verified against
    # app/models/estimate_line_item.py) — every line item participates,
    # no filtering needed.
    line_items_result = await session.execute(
        select(EstimateLineItem, CostCatalogItem)
        .join(CostCatalogItem, EstimateLineItem.cost_catalog_item_id == CostCatalogItem.id)
        .where(EstimateLineItem.estimate_id == estimate_id)
    )
    line_items = line_items_result.all()
    if not line_items:
        return

    catalog_item_ids = [catalog_item.id for _line_item, catalog_item in line_items]
    existing_result = await session.execute(
        select(BomLine).where(
            BomLine.project_id == project_id,
            BomLine.cost_catalog_item_id.in_(catalog_item_ids),
        )
    )
    existing_by_catalog_id = {
        line.cost_catalog_item_id: line for line in existing_result.scalars().all()
    }

    for line_item, catalog_item in line_items:
        existing = existing_by_catalog_id.get(catalog_item.id)
        if existing is not None:
            # Merge: a later approved estimate (e.g. a change-order
            # estimate) needing the same material tops up the existing
            # line's quantity rather than creating a duplicate-looking row.
            existing.quantity = existing.quantity + line_item.quantity
            bom_line_id = existing.id
        else:
            new_line = BomLine(
                company_id=company_id,
                project_id=project_id,
                cost_catalog_item_id=catalog_item.id,
                vendor_id=None,
                description=catalog_item.name,
                unit=catalog_item.unit,
                quantity=line_item.quantity,
                ordered=False,
                ordered_at=None,
                source="estimate",
            )
            session.add(new_line)
            await session.flush()
            existing_by_catalog_id[catalog_item.id] = new_line
            bom_line_id = new_line.id

        await write_audit_log(
            session,
            company_id=company_id,
            actor_id=None,
            action="bom_line.auto_generated",
            entity_type="bom_line",
            entity_id=bom_line_id,
            metadata={"estimate_id": str(estimate_id)},
        )

    await session.flush()
