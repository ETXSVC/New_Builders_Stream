"""Task 4.11 placeholder — Task 4.12 replaces this with the real actor."""
import dramatiq

from app.tasks import broker  # noqa: F401 - import-time side effect


@dramatiq.actor(max_retries=3, actor_name="sync_financial_record")
def sync_financial_record(*, connection_id: str, entity_type: str, entity_id: str) -> None:
    raise NotImplementedError("Task 4.12 implements this actor for real")
