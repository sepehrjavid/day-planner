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
