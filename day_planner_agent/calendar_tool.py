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
import re

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


async def add_calendar_event(
    tool_context: ToolContext,
    summary: str,
    start_time: str,
    end_time: str,
    calendar_summary: str | None = None,
    location: str | None = None,
) -> dict:
    """Create an event on one of the user's connected calendars.

    Args:
        summary: Event title.
        start_time: Start of the event. Give it as local wall-clock time,
            "YYYY-MM-DDTHH:MM:SS", with no UTC offset — it's interpreted in
            the target calendar's own timezone automatically, so never ask
            the user what timezone they're in. Only include a UTC offset
            (e.g. "2026-08-04T20:00:00-07:00") if the user names a specific
            timezone explicitly. Use a bare "YYYY-MM-DD" date for an all-day
            event.
        end_time: End of the event, same format as start_time.
        calendar_summary: Which connected calendar to add it to, matched by
            display name (e.g. "Work"). Omit to use the user's primary
            calendar.
        location: Optional free-text location.

    Returns:
        A dict with "status". On "success", "event" has the created event's
        id, title, times, and a link. On "needs_auth", "connect_url" is a
        link to hand the user. On "not_found", no connected calendar matched
        calendar_summary. On "not_writable", the matched calendar(s) are
        read-only for this user (e.g. a subscribed holiday calendar, or a
        calendar someone else shared without edit access) — tell the user
        rather than guessing at a substitute.
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

    candidates = _candidate_calendars(calendars["calendars"], calendar_summary)
    if not candidates:
        return {
            "status": "not_found",
            "message": (
                f"No connected calendar named {calendar_summary!r}."
                if calendar_summary
                else "No connected calendar to add this to."
            ),
        }

    # Calendar IDs alone don't say who can write to them (a subscribed
    # holiday calendar or a read-only shared calendar looks the same as any
    # other in /internal/calendars) — accessRole only comes back from
    # Google's own calendarList entry, fetched live per candidate below.
    # Reused for the "success" path too, since it also carries timeZone.
    target = None
    entry = None
    saw_any_token = False
    read_only_names = []
    for candidate in candidates:
        token = await backend_client.access_token(user_id, candidate["account_id"])
        if token is None:
            continue
        saw_any_token = True
        try:
            entry = await _fetch_calendar_list_entry(token, candidate["calendar_id"])
        except HttpError as exc:
            return {"status": "error", "error_message": str(exc)}
        if entry.get("accessRole") in _WRITABLE_ROLES:
            target = candidate
            break
        read_only_names.append(entry.get("summary") or candidate["calendar_id"])

    if target is None:
        if not saw_any_token:
            return {
                "status": "needs_auth",
                "message": "That calendar's account needs reconnecting.",
            }
        if calendar_summary:
            return {
                "status": "not_writable",
                "message": f"You only have read access to {calendar_summary!r}.",
            }
        return {
            "status": "not_writable",
            "message": (
                "None of your connected calendars can be written to "
                f"(read-only: {', '.join(read_only_names)})."
            ),
        }

    try:
        event = await _insert_google_event(
            token,
            target["calendar_id"],
            summary,
            start_time,
            end_time,
            location,
            entry.get("timeZone"),
        )
    except HttpError as exc:
        return {"status": "error", "error_message": str(exc)}

    return {"status": "success", "event": event}


async def update_calendar_event(
    tool_context: ToolContext,
    event_id: str,
    calendar_id: str,
    summary: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    location: str | None = None,
) -> dict:
    """Modify an existing event on one of the user's connected calendars.

    event_id and calendar_id identify which event to change — both come
    from a prior get_calendar_events (or add_calendar_event) result's
    "event_id"/"calendar_id" fields. Never guess or ask the user for
    these; look the event up first if you don't already have them from
    earlier in the conversation.

    Only pass the fields that are actually changing — this is a partial
    update, everything else on the event is left as-is. start_time and
    end_time, if given, replace the corresponding field independently (you
    can move just the start, or just the end).

    Args:
        event_id: The event's id, from a prior get_calendar_events or
            add_calendar_event result.
        calendar_id: The calendar the event lives on, from the same prior
            result (its "calendar_id" field).
        summary: New event title, if it's changing.
        start_time: New start time. Same format as add_calendar_event's
            start_time — local wall-clock time with no UTC offset unless
            the user names a specific timezone; resolved automatically
            against the target calendar's own timezone.
        end_time: New end time, same format as start_time.
        location: New free-text location, if it's changing.

    Returns:
        A dict with "status". On "success", "event" has the updated
        event's id, title, times, and a link. On "needs_auth",
        "connect_url" is a link to hand the user. On "not_found", either
        calendar_id doesn't match a connected calendar or event_id doesn't
        exist there — tell the user rather than guessing. On
        "not_writable", the calendar is read-only for this user.
    """
    if not any([summary, start_time, end_time, location]):
        return {"status": "error", "message": "No fields provided to update."}

    user_id = tool_context.session.user_id

    try:
        calendars = await backend_client.list_calendars(user_id)
    except backend_client.NeedsAuth as exc:
        return {
            "status": "needs_auth",
            "connect_url": exc.connect_url,
            "message": exc.message,
        }

    candidate = next(
        (c for c in calendars["calendars"] if c["calendar_id"] == calendar_id), None
    )
    if candidate is None:
        return {
            "status": "not_found",
            "message": f"No connected calendar with id {calendar_id!r}.",
        }

    token = await backend_client.access_token(user_id, candidate["account_id"])
    if token is None:
        return {
            "status": "needs_auth",
            "message": "That calendar's account needs reconnecting.",
        }

    try:
        entry = await _fetch_calendar_list_entry(token, calendar_id)
    except HttpError as exc:
        return {"status": "error", "error_message": str(exc)}

    if entry.get("accessRole") not in _WRITABLE_ROLES:
        return {
            "status": "not_writable",
            "message": f"You only have read access to {entry.get('summary') or calendar_id!r}.",
        }

    try:
        event = await _patch_google_event(
            token,
            calendar_id,
            event_id,
            summary,
            start_time,
            end_time,
            location,
            entry.get("timeZone"),
        )
    except HttpError as exc:
        if exc.resp.status == 404:
            return {
                "status": "not_found",
                "message": f"No event {event_id!r} on that calendar.",
            }
        return {"status": "error", "error_message": str(exc)}

    return {"status": "success", "event": event}


_WRITABLE_ROLES = frozenset({"owner", "writer"})


def _candidate_calendars(calendars: list[dict], calendar_summary: str | None) -> list[dict]:
    if calendar_summary:
        return [
            c for c in calendars if (c.get("summary") or "").lower() == calendar_summary.lower()
        ]

    # Primary first — a user's own primary calendar is always writable, so
    # this is right in the overwhelmingly common case without needing a live
    # access-role check at all. Everything else is a fallback, tried in
    # order, in case the primary itself turns out unwritable or absent.
    primaries = [c for c in calendars if c.get("is_primary")]
    rest = [c for c in calendars if not c.get("is_primary")]
    return primaries + rest


_OFFSET_RE = re.compile(r"(Z|[+-]\d{2}:\d{2})$")


def _is_naive_datetime(value: str) -> bool:
    """True for a timed value with no UTC offset/Z, e.g. "2026-08-04T20:00:00".

    False for a bare date ("2026-08-04", all-day) and for anything that
    already carries an explicit offset.
    """
    return "T" in value and not _OFFSET_RE.search(value)


def _time_field(value: str, calendar_timezone: str | None) -> dict:
    if "T" not in value:
        return {"date": value}
    field = {"dateTime": value}
    if calendar_timezone and _is_naive_datetime(value):
        field["timeZone"] = calendar_timezone
    return field


async def _fetch_calendar_list_entry(access_token: str, calendar_id: str) -> dict:
    """accessRole and timeZone for one calendar, from the *caller's* view of
    it — a plain Calendar resource has no accessRole at all; that only comes
    back on the CalendarList entry, which is per-user by nature.
    """

    def _get() -> dict:
        creds = Credentials(token=access_token)
        service = build("calendar", "v3", credentials=creds)
        return service.calendarList().get(calendarId=calendar_id).execute()

    return await asyncio.to_thread(_get)


async def _insert_google_event(
    access_token: str,
    calendar_id: str,
    summary: str,
    start_time: str,
    end_time: str,
    location: str | None,
    calendar_timezone: str | None,
) -> dict:
    def _insert() -> dict:
        creds = Credentials(token=access_token)
        service = build("calendar", "v3", credentials=creds)
        body = {
            "summary": summary,
            "start": _time_field(start_time, calendar_timezone),
            "end": _time_field(end_time, calendar_timezone),
        }
        if location:
            body["location"] = location
        item = service.events().insert(calendarId=calendar_id, body=body).execute()
        start = item.get("start", {})
        end = item.get("end", {})
        return {
            "event_id": item.get("id"),
            "calendar_id": calendar_id,
            "title": item.get("summary"),
            "start_time": start.get("dateTime", start.get("date")),
            "end_time": end.get("dateTime", end.get("date")),
            "html_link": item.get("htmlLink"),
        }

    # googleapiclient is synchronous; keep it off the event loop like every
    # other blocking call in this codebase.
    return await asyncio.to_thread(_insert)


async def _patch_google_event(
    access_token: str,
    calendar_id: str,
    event_id: str,
    summary: str | None,
    start_time: str | None,
    end_time: str | None,
    location: str | None,
    calendar_timezone: str | None,
) -> dict:
    def _patch() -> dict:
        creds = Credentials(token=access_token)
        service = build("calendar", "v3", credentials=creds)
        body: dict = {}
        if summary is not None:
            body["summary"] = summary
        if start_time is not None:
            body["start"] = _time_field(start_time, calendar_timezone)
        if end_time is not None:
            body["end"] = _time_field(end_time, calendar_timezone)
        if location is not None:
            body["location"] = location
        item = (
            service.events()
            .patch(calendarId=calendar_id, eventId=event_id, body=body)
            .execute()
        )
        start = item.get("start", {})
        end = item.get("end", {})
        return {
            "event_id": item.get("id"),
            "calendar_id": calendar_id,
            "title": item.get("summary"),
            "start_time": start.get("dateTime", start.get("date")),
            "end_time": end.get("dateTime", end.get("date")),
            "html_link": item.get("htmlLink"),
        }

    # googleapiclient is synchronous; keep it off the event loop like every
    # other blocking call in this codebase.
    return await asyncio.to_thread(_patch)


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
