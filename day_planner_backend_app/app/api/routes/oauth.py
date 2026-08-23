"""The browser-facing consent flow.

These two routes are the reason this service exists at all: Vertex AI Agent
Engine is invoke-only and has no public HTTP surface, so it cannot receive an
OAuth redirect. Both are unauthenticated by necessity — Google redirects an
anonymous browser here — and identity rides entirely in the single-use nonce.
"""

import logging

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from ...core.config import Settings, get_settings
from ...db.store import Store
from ...providers import OAuthProvider
from ...services import connections
from ...web import pages
from ..deps import get_provider_or_404, get_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["oauth"])


@router.get("/{provider_name}/start")
async def start(
    s: str,
    provider: OAuthProvider = Depends(get_provider_or_404),
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings),
):
    """Redirect the user's browser to the provider's consent screen.

    The nonce is only *peeked* at here, not consumed — a user who clicks the
    link, gets distracted, and clicks again should not be told it's dead.
    Consumption happens once, at the callback.
    """
    state = await store.oauth_states.peek(s)
    if state is None or state.is_expired or state.provider != provider.name:
        return HTMLResponse(
            pages.failed("That connect link has expired or already been used."),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    url = connections.authorization_url(
        provider=provider, settings=settings, state=state
    )
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


@router.get("/{provider_name}/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    provider: OAuthProvider = Depends(get_provider_or_404),
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings),
):
    """Where the provider sends the browser back after consent."""
    if error:
        # Usually access_denied — the user hit Cancel. Not an incident.
        logger.info("consent declined for %s: %s", provider.name, error)
        return HTMLResponse(
            pages.failed("Consent was declined, so nothing was connected."),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if not code or not state:
        return HTMLResponse(
            pages.failed("That callback was missing required parameters."),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        result = await connections.complete_connection(
            store=store,
            settings=settings,
            provider=provider,
            code=code,
            state_nonce=state,
        )
    except connections.ConnectFailed as exc:
        return HTMLResponse(
            pages.failed(str(exc)), status_code=status.HTTP_400_BAD_REQUEST
        )

    return HTMLResponse(
        pages.connected(
            provider.name,
            result.email,
            result.calendars_found,
            result.calendars_selected,
        )
    )
