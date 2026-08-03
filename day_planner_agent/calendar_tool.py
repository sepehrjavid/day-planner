"""Google Calendar tool for the day planner agent.

Fetches events across every calendar the user has connected via
day_planner_backend_internal — that service owns OAuth token storage,
refresh, and revocation entirely (see ../oauth-design.md). Nothing here ever
handles a Google OAuth credential beyond the ~1-hour access token minted
per call, and nothing here is persisted.

user_id always comes from tool_context.session.user_id, which ADK sets from
the invocation — never from a model-supplied argument. That's the whole
tenant boundary (../oauth-design.md §3): a prompt injection in a calendar
event title cannot make this tool read someone else's schedule, because the
model never gets a chance to say whose schedule to read.
"""

from __future__ import annotations

import asyncio

from google.adk.tools import ToolContext
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import backend_client


async def get_calendar_events(
    tool_context: ToolContext, date_from: str, date_to: str
) -> dict:
    """Retrieve events across every calendar the user has connected, within a date range.

    Args:
        date_from: Start date, inclusive, in YYYY-MM-DD format.
        date_to: End date, exclusive, in YYYY-MM-DD format.

    Returns:
        A dict with "status". On "success", "events" is a merged,
        time-sorted list across every connected and selected calendar, and
        an optional "note" flags any accounts that were skipped. On
        "needs_auth", "connect_url" is a link to hand the user — give it to
        them and stop; do not try to work around missing calendar access.
    """
    user_id = tool_context.session.user_id

    try:
        calendars = await backend_client.list_calendars(user_id)
    except backend_client.NeedsAuth as exc:
        return {
            "status": "needs_auth",
            "connect_url": exc.connect_url,
            "message": exc.message,
        }

    account_ids = {c["account_id"] for c in calendars["calendars"]}
    tokens_by_account = {
        account_id: await backend_client.access_token(user_id, account_id)
        for account_id in account_ids
    }

    events = []
    skipped_accounts = {aid for aid, token in tokens_by_account.items() if token is None}
    for target in calendars["calendars"]:
        token = tokens_by_account.get(target["account_id"])
        if token is None:
            continue
        try:
            events.extend(
                await _fetch_google_events(
                    token, target["calendar_id"], date_from, date_to
                )
            )
        except HttpError as exc:
            return {"status": "error", "error_message": str(exc)}

    events.sort(key=lambda e: e["start_time"] or "")

    result: dict = {"status": "success", "events": events}
    notes = []
    if calendars["needs_reauth"]:
        notes.append(
            f"{len(calendars['needs_reauth'])} connected calendar account(s) "
            "need reconnecting and were skipped."
        )
    if skipped_accounts:
        notes.append(
            f"{len(skipped_accounts)} account(s) went stale mid-request and "
            "were skipped."
        )
    if notes:
        result["note"] = " ".join(notes)
    return result


async def _fetch_google_events(
    access_token: str, calendar_id: str, date_from: str, date_to: str
) -> list[dict]:
    def _list() -> list[dict]:
        # Bearer-token-only Credentials: valid for exactly this call, no
        # refresh_token/token_uri set, so it can't (and doesn't need to)
        # refresh itself — day_planner_backend_internal already handed us a
        # fresh token.
        creds = Credentials(token=access_token)
        service = build("calendar", "v3", credentials=creds)
        response = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=f"{date_from}T00:00:00Z",
                timeMax=f"{date_to}T00:00:00Z",
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        items = []
        for item in response.get("items", []):
            start = item.get("start", {})
            end = item.get("end", {})
            items.append(
                {
                    "event_id": item.get("id"),
                    "title": item.get("summary", "(no title)"),
                    "start_time": start.get("dateTime", start.get("date")),
                    "end_time": end.get("dateTime", end.get("date")),
                    "location": item.get("location"),
                    "calendar_id": calendar_id,
                }
            )
        return items

    # googleapiclient is synchronous; keep it off the event loop like every
    # other blocking call in this codebase.
    return await asyncio.to_thread(_list)
