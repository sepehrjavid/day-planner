"""Client for day_planner_backend_internal — A1.5's user-facing completion
route (see ../api/routes/habit_sessions.py) is the only caller.

Mirrors day_planner_agent/backend_client.py's OIDC pattern: this service's
own ambient service account identity (google_service_account.backend in
terraform, the same identity this service already runs as) mints a
short-lived ID token audienced to the internal service, fresh per call.
No calendar or OAuth credential lives here — day_planner_backend_internal
owns all of that. Only the one route this service actually needs is
wrapped; there's no reason to grow this into a second copy of
backend_client.py.
"""

from __future__ import annotations

import asyncio

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import id_token as google_id_token

from ..core.config import Settings

_TIMEOUT = httpx.Timeout(10.0)


async def _mint_id_token(audience: str) -> str:
    # google-auth's fetch_id_token is synchronous (blocking network I/O) —
    # keep it off the event loop, same reasoning as backend_client.py's
    # identical helper.
    return await asyncio.to_thread(google_id_token.fetch_id_token, Request(), audience)


async def set_habit_session_status(
    settings: Settings,
    *,
    user_id: str,
    calendar_id: str,
    event_id: str,
    status: str,
    marked_by: str,
) -> dict | None:
    """Returns None if no session exists for this (calendar_id, event_id)
    under this user — the route turns that into its own 404, rather than
    this module inventing a different not-found signal per caller."""
    base_url = settings.internal_backend_url.rstrip("/")
    token = await _mint_id_token(base_url)
    async with httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=_TIMEOUT,
    ) as client:
        response = await client.post(
            "/internal/habit-sessions/status",
            json={
                "user_id": user_id,
                "calendar_id": calendar_id,
                "event_id": event_id,
                "status": status,
                "marked_by": marked_by,
            },
        )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()
