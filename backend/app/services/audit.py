import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


async def write_audit_log(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    session.add(
        AuditLog(
            company_id=company_id,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            log_metadata=metadata,
        )
    )
    await session.flush()
