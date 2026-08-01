from pydantic import BaseModel, ConfigDict, EmailStr, Field


class CompanyEmailSettingsResponse(BaseModel):
    """What the screen may see.

    Everything except the password, which is write-only: `has_password`
    says whether one is stored so the form can show "unchanged" rather than
    an empty box that looks like a missing setting. Handing the password
    back would put another company's mail credential into a JSON response,
    a browser cache and any error report that captured it.
    """

    model_config = ConfigDict(from_attributes=True)

    host: str
    port: int
    username: str | None
    from_address: str
    starttls: bool
    enabled: bool
    has_password: bool
    # Null until a test message actually got through, so a saved form is
    # never mistaken for a working one.
    verified_at: str | None = None


class CompanyEmailSettingsPutRequest(BaseModel):
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(default=587, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=255)
    # Omitted (or null) means "keep whatever is stored" — an empty string
    # means "remove it". Without that distinction a form that cannot show
    # the current password could only ever clear it.
    password: str | None = None
    from_address: EmailStr
    starttls: bool = True
    enabled: bool = True


class EmailSettingsTestResponse(BaseModel):
    """The result of trying it for real.

    `detail` carries the mail server's own words on failure, because "the
    relay said 535 authentication failed" is the difference between fixing
    a password and guessing.
    """

    ok: bool
    detail: str
