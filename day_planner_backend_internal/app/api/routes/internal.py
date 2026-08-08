"""Service-to-service routes, called by the agent runtime and Slack adapter.

Unlike /me, these take `user_id` in the request body — the caller is a trusted
service acting on a user's behalf. That trust has one condition, and it lives
on the agent side: the value must come from `tool_context.session.user_id`,
which Agent Engine sets from the invocation, and never from anything the model
produced. A model-supplied user_id here means a prompt injection in a calendar
event title can read someone else's schedule.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from ...core.config import Settings, get_settings
from ...db.models import STATUS_ACTIVE
from ...db.store import Store
from ...schemas.calendars import (
    AccessTokenRequest,
    AccessTokenResponse,
    CalendarsResponse,
    CalendarTarget,
    ConnectLinkRequest,
    ConnectLinkResponse,
    DisconnectRequest,
    RemoveCalendarRequest,
)
from ...services import connections
from ..deps import (
    get_store,
    provider_factory,
    require_internal_caller,
    require_known_provider,
)

router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(require_internal_caller)],
)


@router.post("/connect-link", response_model=ConnectLinkResponse)
async def connect_link(
    body: ConnectLinkRequest,
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings),
):
    """Mint a single-use connect link on a user's behalf.

    Deliver it privately — a DM or an ephemeral Slack message. Anyone who can
    click this link attaches *their* calendar account to this user_id.
    """
    known = require_known_provider(body.provider, settings)
    link = await connections.mint_connect_link(
        store=store, settings=settings, user_id=body.user_id, provider=known.name
    )
    return ConnectLinkResponse(
        connect_url=link.connect_url, expires_at=link.expires_at
    )


@router.get("/calendars", response_model=CalendarsResponse)
async def list_selected_calendars(user_id: str, store: Store = Depends(get_store)):
    """Every calendar the planner should consider, across every linked account.

    This is what makes multi-calendar planning work: the agent asks once and
    gets a flat list spanning personal and work accounts, each tagged with the
    account_id whose access token unlocks it.
    """
    accounts = await store.list_accounts(user_id)

    targets = [
        CalendarTarget(
            account_id=account.account_id,
            provider=account.provider,
            account_email=account.email,
            calendar_id=calendar.calendar_id,
            summary=calendar.summary,
            is_primary=calendar.is_primary,
        )
        for account in accounts
        if account.status == STATUS_ACTIVE
        for calendar in account.calendars
        if calendar.selected
    ]

    return CalendarsResponse(
        connected=bool(targets),
        # Surfaced rather than hidden: a user whose work calendar quietly
        # stopped refreshing should be told, not silently planned around.
        needs_reauth=[a.account_id for a in accounts if a.status != STATUS_ACTIVE],
        calendars=targets,
    )


@router.post("/access-token", response_model=AccessTokenResponse)
async def access_token(
    body: AccessTokenRequest,
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings),
    provider_for=Depends(provider_factory),
):
    """Mint a short-lived provider access token for one connected account.

    Omitting account_id uses the user's default account. A 409 means the user
    must (re)connect — callers should surface the connect-link flow rather than
    treating it as a failure.
    """
    try:
        token = await connections.mint_access_token(
            store=store,
            settings=settings,
            provider_for=provider_for,
            user_id=body.user_id,
            account_id=body.account_id,
        )
    except connections.NotConnected as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "not_connected", "account_id": body.account_id},
        ) from exc
    except connections.ReauthRequired as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "needs_reauth",
                "account_id": exc.args[0] if exc.args else body.account_id,
            },
        ) from exc
    except connections.OAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "provider_error"},
        ) from exc

    return AccessTokenResponse(
        account_id=token.account_id,
        access_token=token.access_token,
        expires_at=token.expires_at,
        scopes=token.scopes,
    )


@router.post("/remove-calendar")
async def remove_calendar(
    body: RemoveCalendarRequest,
    store: Store = Depends(get_store),
):
    """Drop a calendar the agent found 404ing on Google's side (deleted or
    unsubscribed) from the stored list, so it stops being retried on every
    future request.

    404s if the account itself is gone — otherwise idempotent, since the
    calendar may already be absent (e.g. two concurrent requests both hit
    the same stale entry).
    """
    account = await store.remove_calendar(
        user_id=body.user_id, account_id=body.account_id, calendar_id=body.calendar_id
    )
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return {"status": "removed"}


@router.post("/disconnect")
async def disconnect(
    body: DisconnectRequest,
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings),
    provider_for=Depends(provider_factory),
):
    removed = await connections.disconnect_account(
        store=store,
        settings=settings,
        provider_for=provider_for,
        user_id=body.user_id,
        account_id=body.account_id,
    )
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return {"status": "disconnected", "account_id": body.account_id}
