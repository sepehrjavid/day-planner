"""Request/response models for calendar accounts."""

from datetime import datetime

from pydantic import BaseModel, Field

from ..db.models import ConnectedAccount


class CalendarOut(BaseModel):
    calendar_id: str
    summary: str | None
    is_primary: bool
    selected: bool


class AccountOut(BaseModel):
    account_id: str
    provider: str
    email: str | None
    status: str
    calendars: list[CalendarOut]

    @staticmethod
    def of(account: ConnectedAccount) -> "AccountOut":
        return AccountOut(
            account_id=account.account_id,
            provider=account.provider,
            email=account.email,
            status=account.status,
            calendars=[
                CalendarOut(
                    calendar_id=c.calendar_id,
                    summary=c.summary,
                    is_primary=c.is_primary,
                    selected=c.selected,
                )
                for c in account.calendars
            ],
        )


class MeResponse(BaseModel):
    user_id: str
    email: str
    default_account_id: str | None
    accounts: list[AccountOut]


class CalendarSelectionRequest(BaseModel):
    selected_calendar_ids: list[str] = Field(default_factory=list)


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
