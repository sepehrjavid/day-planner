"""Routes for the signed-in user managing their own calendar accounts.

Every handler here takes user_id from `current_user_id` (the session token) and
never from the request. That's what makes `account_id` safe to accept as a path
parameter: it is always resolved *within* the caller's own subcollection, so
another user's id is simply a 404.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from ...core.config import Settings, get_settings
from ...db.store import Store
from ...schemas.calendars import (
    AccountOut,
    CalendarSelectionRequest,
    ConnectLinkResponse,
    MeResponse,
)
from ...services import connections
from ..deps import current_user_id, get_store, provider_factory, require_known_provider

router = APIRouter(prefix="/me", tags=["me"])


@router.get("", response_model=MeResponse)
async def read_me(
    user_id: str = Depends(current_user_id), store: Store = Depends(get_store)
):
    user = await store.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    return MeResponse(
        user_id=user_id,
        email=user["email"],
        default_account_id=user.get("default_account_id"),
        accounts=[AccountOut.of(a) for a in await store.list_accounts(user_id)],
    )


@router.post("/calendar-accounts/connect-link", response_model=ConnectLinkResponse)
async def create_connect_link(
    provider: str = "google",
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings),
):
    """Start linking another calendar account to the signed-in user."""
    known = require_known_provider(provider, settings)
    link = await connections.mint_connect_link(
        store=store, settings=settings, user_id=user_id, provider=known.name
    )
    return ConnectLinkResponse(
        connect_url=link.connect_url, expires_at=link.expires_at
    )


@router.patch("/calendar-accounts/{account_id}/calendars", response_model=AccountOut)
async def select_calendars(
    account_id: str,
    body: CalendarSelectionRequest,
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
):
    """Choose which of this account's calendars the planner should consider."""
    updated = await store.set_calendar_selection(
        user_id=user_id,
        account_id=account_id,
        selected_calendar_ids=set(body.selected_calendar_ids),
    )
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return AccountOut.of(updated)


@router.delete("/calendar-accounts/{account_id}", status_code=204)
async def disconnect_account(
    account_id: str,
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings),
    provider_for=Depends(provider_factory),
):
    removed = await connections.disconnect_account(
        store=store,
        settings=settings,
        provider_for=provider_for,
        user_id=user_id,
        account_id=account_id,
    )
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
