import uuid

from pydantic import BaseModel, ConfigDict, Field


class PhaseCreateRequest(BaseModel):
    """Body for `POST /projects/{id}/phases` (Task 1.14)."""

    name: str = Field(..., min_length=1, max_length=255)
    # DB default is 0 (docs/04-database-schema.md Section 4:
    # `sequence INT NOT NULL DEFAULT 0`); optional here so a caller can omit
    # it and get the same default the column itself would apply.
    sequence: int = Field(0, ge=0)


class PhaseResponse(BaseModel):
    """Full model. `phases` has no created_at/updated_at columns
    (docs/04-database-schema.md Section 4) — not an omission, the schema
    doc simply doesn't track timestamps for this table."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    company_id: uuid.UUID
    name: str
    sequence: int
