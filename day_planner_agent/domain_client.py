"""HTTP client for day_planner_backend_app's /agent/* routes — domain
data only: habits, habit sessions, zones, and the sleep schedule.

A6.1 moved this data's storage from day_planner_backend_internal to
day_planner_backend_app (the public-facing service). A6.2 is what gives
the agent a path to it there: a new /agent/* route group, gated by an
OIDC-verified, allowlisted service identity — the same trust model
backend_client.py already used against the internal service, just a
different audience. See backend_client.py's own module docstring for
why the two clients must never be confused, and _service_client.py for
the shared token-caching/connection-pooling machinery both are built on.

user_id is passed explicitly to every function here, the same as
backend_client.py — always from tool_context.session.user_id on the
caller's side (zone_tools.py, habit_tools.py, calendar_tool.py), never
from anything the model produced. Agent routes trust user_id in the
request body precisely because the caller's own identity (this OIDC
token) is independently verified — see
day_planner_backend_app/app/schemas/agent.py's AgentUserRequest.
"""

from __future__ import annotations

import os

from . import _service_client
from ._service_client import BACKEND_ERROR  # re-exported for callers

APP_BACKEND_URL = os.environ["APP_BACKEND_URL"].rstrip("/")

_client = _service_client.ServiceClient(APP_BACKEND_URL)


async def create_habit(user_id: str, *, label: str, goal: str) -> dict:
    response = await _client.post(
        "/agent/habits", json={"user_id": user_id, "label": label, "goal": goal}
    )
    response.raise_for_status()
    return response.json()


async def list_habits(user_id: str, *, status: str | None = None) -> list[dict]:
    params: dict = {"user_id": user_id}
    if status is not None:
        params["status"] = status
    response = await _client.get("/agent/habits", params=params)
    response.raise_for_status()
    return response.json()["habits"]


async def update_habit(
    user_id: str,
    habit_id: str,
    *,
    label: str | None = None,
    goal: str | None = None,
    status: str | None = None,
    allowed_zones: list[str] | None = None,
) -> dict | None:
    """Returns None if habit_id doesn't exist for this user (backend 404)."""
    body: dict = {"user_id": user_id, "habit_id": habit_id}
    if label is not None:
        body["label"] = label
    if goal is not None:
        body["goal"] = goal
    if status is not None:
        body["status"] = status
    if allowed_zones is not None:
        body["allowed_zones"] = allowed_zones
    response = await _client.post("/agent/habits/update", json=body)
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
    response = await _client.post(
        "/agent/habit-sessions",
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


async def set_habit_session_status(
    user_id: str,
    *,
    calendar_id: str,
    event_id: str,
    status: str,
    marked_by: str = "agent",
) -> dict | None:
    """Returns None if no session is logged for this (calendar_id,
    event_id) under this user (backend 404)."""
    response = await _client.post(
        "/agent/habit-sessions/status",
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


async def list_habit_sessions(
    user_id: str, *, planned_from: str, planned_to: str
) -> list[dict]:
    response = await _client.get(
        "/agent/habit-sessions",
        params={"user_id": user_id, "planned_from": planned_from, "planned_to": planned_to},
    )
    response.raise_for_status()
    return response.json()["sessions"]


async def create_zone(
    user_id: str, *, label: str, start_time: str, end_time: str, days_of_week: list[str]
) -> dict:
    response = await _client.post(
        "/agent/zones",
        json={
            "user_id": user_id,
            "label": label,
            "start_time": start_time,
            "end_time": end_time,
            "days_of_week": days_of_week,
        },
    )
    response.raise_for_status()
    return response.json()


async def list_zones(user_id: str) -> list[dict]:
    response = await _client.get("/agent/zones", params={"user_id": user_id})
    response.raise_for_status()
    return response.json()["zones"]


async def update_zone(
    user_id: str,
    zone_id: str,
    *,
    label: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    days_of_week: list[str] | None = None,
) -> dict | None:
    """Returns None if zone_id doesn't exist for this user (backend 404)."""
    body: dict = {"user_id": user_id, "zone_id": zone_id}
    if label is not None:
        body["label"] = label
    if start_time is not None:
        body["start_time"] = start_time
    if end_time is not None:
        body["end_time"] = end_time
    if days_of_week is not None:
        body["days_of_week"] = days_of_week
    response = await _client.post("/agent/zones/update", json=body)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


async def get_sleep_schedule(user_id: str) -> dict | None:
    """Returns None if the user has never set a sleep schedule."""
    response = await _client.get("/agent/sleep-schedule", params={"user_id": user_id})
    response.raise_for_status()
    body = response.json()
    return body["schedule"] if body["exists"] else None


async def set_sleep_schedule(
    user_id: str,
    *,
    sleep_time: str | None = None,
    wake_time: str | None = None,
    cool_down_minutes: int | None = None,
    wake_up_buffer_minutes: int | None = None,
    day_overrides: dict[str, dict[str, str]] | None = None,
) -> dict:
    """Create-or-update — always succeeds, the first call for a user
    creates the schedule rather than needing a separate create step."""
    body: dict = {"user_id": user_id}
    if sleep_time is not None:
        body["sleep_time"] = sleep_time
    if wake_time is not None:
        body["wake_time"] = wake_time
    if cool_down_minutes is not None:
        body["cool_down_minutes"] = cool_down_minutes
    if wake_up_buffer_minutes is not None:
        body["wake_up_buffer_minutes"] = wake_up_buffer_minutes
    if day_overrides is not None:
        body["day_overrides"] = day_overrides
    response = await _client.post("/agent/sleep-schedule", json=body)
    response.raise_for_status()
    return response.json()
