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
import os

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import id_token as google_id_token

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


async def create_habit(user_id: str, *, label: str, goal: str) -> dict:
    async with await _client() as client:
        response = await client.post(
            "/internal/habits", json={"user_id": user_id, "label": label, "goal": goal}
        )
    response.raise_for_status()
    return response.json()


async def list_habits(user_id: str, *, status: str | None = None) -> list[dict]:
    params: dict = {"user_id": user_id}
    if status is not None:
        params["status"] = status
    async with await _client() as client:
        response = await client.get("/internal/habits", params=params)
    response.raise_for_status()
    return response.json()["habits"]


async def update_habit(
    user_id: str,
    habit_id: str,
    *,
    label: str | None = None,
    goal: str | None = None,
    status: str | None = None,
) -> dict | None:
    """Returns None if habit_id doesn't exist for this user (backend 404)."""
    body: dict = {"user_id": user_id, "habit_id": habit_id}
    if label is not None:
        body["label"] = label
    if goal is not None:
        body["goal"] = goal
    if status is not None:
        body["status"] = status
    async with await _client() as client:
        response = await client.post("/internal/habits/update", json=body)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


async def upsert_habit_session(
    user_id: str,
    *,
    habit_id: str,
    event_id: str,
    calendar_id: str,
    planned_start: str,
    planned_end: str,
) -> dict:
    async with await _client() as client:
        response = await client.post(
            "/internal/habit-sessions",
            json={
                "user_id": user_id,
                "habit_id": habit_id,
                "event_id": event_id,
                "calendar_id": calendar_id,
                "planned_start": planned_start,
                "planned_end": planned_end,
            },
        )
    response.raise_for_status()
    return response.json()


async def list_habit_sessions(
    user_id: str, *, planned_from: str, planned_to: str
) -> list[dict]:
    async with await _client() as client:
        response = await client.get(
            "/internal/habit-sessions",
            params={"user_id": user_id, "planned_from": planned_from, "planned_to": planned_to},
        )
    response.raise_for_status()
    return response.json()["sessions"]
