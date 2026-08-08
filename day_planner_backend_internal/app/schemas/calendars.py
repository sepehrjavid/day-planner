"""Request/response models for /internal/*.

No AccountOut/MeResponse/CalendarSelectionRequest here — those back the
app-facing /me/* routes, which don't exist in this codebase.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class ConnectLinkResponse(BaseModel):
    connect_url: str
    expires_at: datetime


class InternalUserRequest(BaseModel):
    # NOTE for the agent integration: this must be the value read from
    # `tool_context.session.user_id`, never one the model produced. It is the
    # whole tenant boundary.
    user_id: str = Field(min_length=1, max_length=256)


class ConnectLinkRequest(InternalUserRequest):
    provider: str = "google"


class AccessTokenRequest(InternalUserRequest):
    account_id: str | None = None


class AccessTokenResponse(BaseModel):
    account_id: str
    access_token: str
    expires_at: datetime
    scopes: list[str]


class DisconnectRequest(InternalUserRequest):
    account_id: str


class RemoveCalendarRequest(InternalUserRequest):
    account_id: str
    calendar_id: str


class CalendarTarget(BaseModel):
    account_id: str
    provider: str
    account_email: str | None
    calendar_id: str
    summary: str | None
    is_primary: bool


class CalendarsResponse(BaseModel):
    connected: bool
    needs_reauth: list[str] = Field(default_factory=list)
    calendars: list[CalendarTarget] = Field(default_factory=list)
