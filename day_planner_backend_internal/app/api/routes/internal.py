"""Service-to-service routes, called by the agent runtime and Slack adapter.

Unlike /me, these take `user_id` in the request body — the caller is a trusted
service acting on a user's behalf. That trust has one condition, and it lives
on the agent side: the value must come from `tool_context.session.user_id`,
which Agent Engine sets from the invocation, and never from anything the model
produced. A model-supplied user_id here means a prompt injection in a calendar
event title can read someone else's schedule.
"""

from datetime import datetime

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
)
from ...schemas.habit_sessions import (
    HabitSessionOut,
    HabitSessionsResponse,
    UpsertHabitSessionRequest,
)
from ...schemas.habits import (
    CreateHabitRequest,
    HabitOut,
    HabitsResponse,
    UpdateHabitRequest,
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


def _to_habit_out(habit) -> HabitOut:
    return HabitOut(
        habit_id=habit.habit_id,
        label=habit.label,
        goal=habit.goal,
        status=habit.status,
        created_at=habit.created_at,
        updated_at=habit.updated_at,
    )


@router.post("/habits", response_model=HabitOut)
async def create_habit(body: CreateHabitRequest, store: Store = Depends(get_store)):
    """Track a new recurring goal for a user."""
    habit = await store.create_habit(user_id=body.user_id, label=body.label, goal=body.goal)
    return _to_habit_out(habit)


@router.get("/habits", response_model=HabitsResponse)
async def list_habits(
    user_id: str, status: str | None = None, store: Store = Depends(get_store)
):
    """Every tracked habit for a user, optionally filtered to one status.

    Filtering (and any "active by default" policy) is the agent tool's
    call, not this route's — this just passes the filter through as given.
    """
    habits = await store.list_habits(user_id, status=status)
    return HabitsResponse(habits=[_to_habit_out(h) for h in habits])


@router.post("/habits/update", response_model=HabitOut)
async def update_habit(body: UpdateHabitRequest, store: Store = Depends(get_store)):
    """Partial update of a tracked habit, e.g. to change its goal or retire
    it (status="paused"/"archived") — never a hard delete, so anything that
    already referenced this habit_id (a tagged calendar event, a plan log
    entry) still resolves."""
    habit = await store.update_habit(
        user_id=body.user_id,
        habit_id=body.habit_id,
        label=body.label,
        goal=body.goal,
        status=body.status,
    )
    if habit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _to_habit_out(habit)


def _to_habit_session_out(session) -> HabitSessionOut:
    return HabitSessionOut(
        session_id=session.session_id,
        habit_id=session.habit_id,
        event_id=session.event_id,
        calendar_id=session.calendar_id,
        planned_start=session.planned_start,
        planned_end=session.planned_end,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


@router.post("/habit-sessions", response_model=HabitSessionOut)
async def upsert_habit_session(
    body: UpsertHabitSessionRequest, store: Store = Depends(get_store)
):
    """Log (or, for an event already logged, update) the plan for one
    calendar event created for a habit. Called by add_calendar_event on
    creation and by update_calendar_event when it moves an already-tagged
    event — see calendar_tool.py."""
    session = await store.upsert_habit_session(
        user_id=body.user_id,
        habit_id=body.habit_id,
        event_id=body.event_id,
        calendar_id=body.calendar_id,
        planned_start=body.planned_start,
        planned_end=body.planned_end,
    )
    return _to_habit_session_out(session)


@router.get("/habit-sessions", response_model=HabitSessionsResponse)
async def list_habit_sessions(
    user_id: str,
    planned_from: datetime,
    planned_to: datetime,
    store: Store = Depends(get_store),
):
    """Every session planned to start in [planned_from, planned_to) —
    review_habit_week's input. Sessions whose event was later deleted are
    still returned; that's the entire point of this record surviving
    independently of the calendar event it describes."""
    sessions = await store.list_habit_sessions(
        user_id, planned_from=planned_from, planned_to=planned_to
    )
    return HabitSessionsResponse(sessions=[_to_habit_session_out(s) for s in sessions])
