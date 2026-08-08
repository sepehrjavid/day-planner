"""HTTP client for day_planner_backend_internal.

Owns the one thing every call to the internal backend needs: minting this
runtime's own OIDC identity token. No calendar or OAuth credential lives
here at all — day_planner_backend_internal owns all of that; this module
only ever holds a short-lived bearer token for authenticating *to* the
backend, minted fresh per call from this runtime's own service account via
the metadata server (works on any GCP compute environment, Agent Engine
included, with zero credentials checked into this codebase).
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import id_token as google_id_token

logger = logging.getLogger(__name__)

INTERNAL_BACKEND_URL = os.environ["INTERNAL_BACKEND_URL"].rstrip("/")

_TIMEOUT = httpx.Timeout(10.0)


class NeedsAuth(Exception):
    """No usable calendar connection for this user. Carries the link to send them."""

    def __init__(self, connect_url: str, message: str) -> None:
        self.connect_url = connect_url
        self.message = message
        super().__init__(message)


async def _mint_id_token() -> str:
    # google-auth's fetch_id_token is synchronous (blocking network I/O);
    # every ADK tool in this codebase is async, so this always runs off the
    # event loop rather than stalling it.
    return await asyncio.to_thread(
        google_id_token.fetch_id_token, Request(), INTERNAL_BACKEND_URL
    )


async def _client() -> httpx.AsyncClient:
    token = await _mint_id_token()
    return httpx.AsyncClient(
        base_url=INTERNAL_BACKEND_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=_TIMEOUT,
    )


async def connect_link(user_id: str, *, provider: str = "google") -> str:
    async with await _client() as client:
        response = await client.post(
            "/internal/connect-link", json={"user_id": user_id, "provider": provider}
        )
    response.raise_for_status()
    return response.json()["connect_url"]


async def list_calendars(user_id: str) -> dict:
    """Every selected calendar across every connected account, plus which
    accounts (if any) have gone stale and need reconnecting.

    Raises NeedsAuth if nothing is connected yet at all.
    """
    async with await _client() as client:
        response = await client.get("/internal/calendars", params={"user_id": user_id})
    response.raise_for_status()
    body = response.json()

    if not body["connected"]:
        url = await connect_link(user_id)
        raise NeedsAuth(url, "No calendar is connected yet.")

    return body


async def access_token(user_id: str, account_id: str) -> str | None:
    """A short-lived provider access token for one connected account.

    Returns None (rather than raising) if that specific account has gone
    stale — /internal/calendars already filters to active accounts before
    this is called, so a 409 here means one went stale in the brief window
    between the two calls. The caller decides whether to skip just that
    account's calendars or treat it as fatal.
    """
    async with await _client() as client:
        response = await client.post(
            "/internal/access-token",
            json={"user_id": user_id, "account_id": account_id},
        )
    if response.status_code == 409:
        return None
    response.raise_for_status()
    return response.json()["access_token"]


async def remove_calendar(user_id: str, account_id: str, calendar_id: str) -> None:
    """Tell the backend a calendar 404d on Google's side (deleted or
    unsubscribed) so it stops being retried on every future request.

    Best-effort cleanup, not part of the tool call's own success/failure —
    the caller has already decided to skip this calendar regardless, so a
    failure here is logged and swallowed rather than propagated.
    """
    try:
        async with await _client() as client:
            response = await client.post(
                "/internal/remove-calendar",
                json={"user_id": user_id, "account_id": account_id, "calendar_id": calendar_id},
            )
        response.raise_for_status()
    except httpx.HTTPError:
        logger.warning(
            "remove_calendar failed for account_id=%s calendar_id=%s",
            account_id,
            calendar_id,
            exc_info=True,
        )
