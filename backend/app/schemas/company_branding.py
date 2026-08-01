from pydantic import BaseModel, ConfigDict, Field


class CompanyBrandingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    logo_storage_path: str | None
    accent_color: str
    footer_text: str
    # Migration 0027. Empty means "send as the company's own name" — the
    # resolution lives in app/services/email_sender.py, so this field is
    # returned as stored rather than pre-resolved: a screen showing the
    # placeholder needs to know the difference between "not set" and "set to
    # something that happens to match the company name".
    email_sender_name: str


class CompanyBrandingPutRequest(BaseModel):
    accent_color: str = Field(..., pattern=r"^#[0-9a-fA-F]{6}$")
    footer_text: str = ""
    # 120 chars matches the column. No character restrictions beyond the
    # length: `formataddr` quotes and RFC 2047-encodes whatever is here when
    # it builds the From header, so a comma, an apostrophe or an umlaut is
    # safe to store as typed rather than something to reject at the edge.
    email_sender_name: str = Field(default="", max_length=120)
