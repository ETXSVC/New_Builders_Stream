from pydantic import BaseModel, ConfigDict, Field


class CompanyBrandingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    logo_storage_path: str | None
    accent_color: str
    footer_text: str


class CompanyBrandingPutRequest(BaseModel):
    accent_color: str = Field(..., pattern=r"^#[0-9a-fA-F]{6}$")
    footer_text: str = ""
