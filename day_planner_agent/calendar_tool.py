"""Google Calendar tool for the day planner agent.

Fetches events across every calendar the user has connected via
day_planner_backend_internal — that service owns OAuth token storage,
refresh, and revocation entirely (see ../docs/oauth-design.md). Nothing here ever
handles a Google OAuth credential beyond the ~1-hour access token minted
per call, and nothing here is persisted.

user_id always comes from tool_context.session.user_id, which ADK sets from
the invocation — never from a model-supplied argument. That's the whole
tenant boundary (../docs/oauth-design.md §3): a prompt injection in a calendar
event title cannot make this tool read someone else's schedule, because the
model never gets a chance to say whose schedule to read.
"""

from __future__ import annotations

import asyncio
import logging
import re

from google.adk.tools import ToolContext
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import backend_client

logger = logging.getLogger(__name__)

# Extended-property keys used to tag a Google Calendar event as one the
# agent placed for a habit (see add_calendar_event's habit_id param and
# docs/feature-ideas.md item 2). Private extended properties are only ever
# visible to the app that set them, never to the user in the Calendar UI.
_HABIT_ID_PROPERTY = "day_planner_habit_id"

# Google's events().list returns at most 250 items per page. These caps are
# a safety net against an unbounded loop (e.g. a misbehaving or malicious
# calendar backend that never stops returning nextPageToken), not a limit
# expected to be hit in normal use.
_MAX_EVENT_PAGES = 20
_MAX_EVENTS = 5000


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
        an optional "note" flags any accounts that were skipped. Each event
        carries "habit_id" when it's a session previously placed for a
        tracked habit (see add_calendar_event's habit_id param) — the key
        is absent entirely for a plain event, so check for its presence
        rather than assuming it's always there. This is how you notice an
        already-scheduled habit session colliding with something you've
        just learned, e.g. a newly-stated work-hours preference (see
        instruction.md). On "needs_auth", "connect_url" is a link to hand
        the user — give it to them and stop; do not try to work around
        missing calendar access.
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

    # Sorted for reproducibility only — the original dict comprehension
    # this replaces iterated a set too, so there was never an ordering
    # guarantee to preserve here, unlike events.sort below.
    account_ids = sorted({c["account_id"] for c in calendars["calendars"]})
    tokens = await asyncio.gather(
        *(backend_client.access_token(user_id, account_id) for account_id in account_ids)
    )
    tokens_by_account = dict(zip(account_ids, tokens))

    skipped_accounts = {aid for aid, token in tokens_by_account.items() if token is None}
    fetch_targets = [
        target
        for target in calendars["calendars"]
        if tokens_by_account.get(target["account_id"]) is not None
    ]

    # return_exceptions=True so one calendar's HttpError doesn't cancel
    # the others still in flight — but the original serial loop returned
    # {"status": "error"} on the *first* HttpError and never reached the
    # rest, so that's reproduced deliberately below rather than silently
    # becoming "collect every calendar's error" or "ignore calendar-level
    # failures". Anything that isn't an HttpError re-raises, matching the
    # original code's total silence on any other exception type — it was
    # never caught before, so it must not start being swallowed now.
    fetch_results = await asyncio.gather(
        *(
            _fetch_google_events(
                tokens_by_account[target["account_id"]],
                target["calendar_id"],
                date_from,
                date_to,
            )
            for target in fetch_targets
        ),
        return_exceptions=True,
    )

    events = []
    for result in fetch_results:
        if isinstance(result, HttpError):
            return {"status": "error", "error_message": str(result)}
        if isinstance(result, BaseException):
            raise result
        events.extend(result)

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
    habit_id: str | None = None,
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
        habit_id: Pass this when — and only when — this event is a session
            you're placing for a tracked habit (see instruction.md's habit
            placement guidance and habit_tools.py's create_habit/
            list_habits). It tags the event so review_habit_week can find
            it later and logs the plan; never set it for a plain
            user-stated appointment. The logging is best-effort — a
            failure here does not fail event creation, so don't retry or
            report an error to the user over it.

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
        calendars = await _cached_list_calendars(tool_context, user_id)
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
            entry = await _cached_calendar_list_entry(
                tool_context, candidate["account_id"], token, candidate["calendar_id"]
            )
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
            extended_properties={_HABIT_ID_PROPERTY: habit_id} if habit_id else None,
        )
    except HttpError as exc:
        return {"status": "error", "error_message": str(exc)}

    if habit_id:
        await _log_habit_session(user_id, habit_id, event)

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
        event, tagged_habit_id = await _patch_google_event(
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

    # Only re-log the plan if the time actually moved — a summary/location
    # edit on a habit-tagged event doesn't change when it's happening, so
    # there's nothing for review_habit_week's comparison to need updated.
    if tagged_habit_id and (start_time is not None or end_time is not None):
        await _log_habit_session(user_id, tagged_habit_id, event)

    return {"status": "success", "event": event}


async def delete_calendar_event(
    tool_context: ToolContext,
    event_id: str,
    calendar_id: str,
) -> dict:
    """Delete an existing event from one of the user's connected calendars.

    event_id and calendar_id identify which event to remove — both come
    from a prior get_calendar_events (or add_calendar_event) result's
    "event_id"/"calendar_id" fields. Never guess or ask the user for
    these; look the event up first if you don't already have them from
    earlier in the conversation. This is irreversible — confirm with the
    user before calling this.

    Args:
        event_id: The event's id, from a prior get_calendar_events or
            add_calendar_event result.
        calendar_id: The calendar the event lives on, from the same prior
            result (its "calendar_id" field).

    Returns:
        A dict with "status". On "success", the event was deleted. On
        "needs_auth", "connect_url" is a link to hand the user. On
        "not_found", either calendar_id doesn't match a connected calendar
        or event_id doesn't exist there — tell the user rather than
        guessing. On "not_writable", the calendar is read-only for this
        user.
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
        await _delete_google_event(token, calendar_id, event_id)
    except HttpError as exc:
        if exc.resp.status == 404:
            return {
                "status": "not_found",
                "message": f"No event {event_id!r} on that calendar.",
            }
        return {"status": "error", "error_message": str(exc)}

    return {"status": "success"}


_WRITABLE_ROLES = frozenset({"owner", "writer"})


def _invocation_cache_key(tool_context: ToolContext) -> str:
    # tool_context.invocation_id is shared across every tool call ADK
    # makes while processing one user message (one turn) — that's what
    # scopes this cache to "this invocation only", per A2.2's explicit
    # "do not cache across turns or sessions". A stale entry from an
    # earlier turn just never matches the current invocation_id; nothing
    # actively evicts it, but state per session stays small enough that
    # this hasn't needed to matter.
    return f"calendar_tool:invocation_cache:{tool_context.invocation_id}"


async def _cached_list_calendars(tool_context: ToolContext, user_id: str) -> dict:
    """Memoized per invocation — add_calendar_event calling this once per
    habit session placed in a turn (seven for seven sessions) was
    repeating an identical backend_client.list_calendars call each time
    (A2.2). Not memoized in get_calendar_events, which only ever calls
    this once per turn already."""
    cache = tool_context.state.setdefault(_invocation_cache_key(tool_context), {})
    if "calendars" not in cache:
        cache["calendars"] = await backend_client.list_calendars(user_id)
    return cache["calendars"]


async def _cached_calendar_list_entry(
    tool_context: ToolContext, account_id: str, access_token: str, calendar_id: str
) -> dict:
    """Memoized per invocation, keyed on (account_id, calendar_id) —
    deliberately *not* calendar_id alone. accessRole is per-caller by
    nature (see _fetch_calendar_list_entry's own docstring: a plain
    Calendar resource has no accessRole at all, only the caller's own
    CalendarList entry does), so the same calendar_id shared across two
    of the user's connected accounts can carry a different accessRole for
    each — keying on calendar_id alone would let one account's cached
    role silently answer the other account's lookup."""
    cache = tool_context.state.setdefault(_invocation_cache_key(tool_context), {})
    entries = cache.setdefault("calendar_list_entries", {})
    key = f"{account_id}:{calendar_id}"
    if key not in entries:
        entries[key] = await _fetch_calendar_list_entry(access_token, calendar_id)
    return entries[key]


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


def _extract_habit_id(item: dict) -> str | None:
    return (item.get("extendedProperties") or {}).get("private", {}).get(
        _HABIT_ID_PROPERTY
    )


def _trim_google_event(item: dict, calendar_id: str) -> dict:
    start = item.get("start", {})
    end = item.get("end", {})
    trimmed = {
        "event_id": item.get("id"),
        "calendar_id": calendar_id,
        "title": item.get("summary"),
        "start_time": start.get("dateTime", start.get("date")),
        "end_time": end.get("dateTime", end.get("date")),
        "html_link": item.get("htmlLink"),
    }
    # Only present when this event was tagged for a habit — a plain event's
    # dict has no "habit_id" key at all, rather than an explicit null, so a
    # simple presence check ("habit_id" in event / event.get("habit_id"))
    # works everywhere this shape shows up (get/add/update_calendar_event).
    habit_id = _extract_habit_id(item)
    if habit_id:
        trimmed["habit_id"] = habit_id
    return trimmed


async def _log_habit_session(user_id: str, habit_id: str, event: dict) -> None:
    """Best-effort: logging the plan must never fail event creation/
    rescheduling itself — a missed log entry just means one session is
    invisible to a future review_habit_week, not a broken calendar."""
    try:
        await backend_client.upsert_habit_session(
            user_id,
            habit_id=habit_id,
            event_id=event["event_id"],
            calendar_id=event["calendar_id"],
            planned_start=event["start_time"],
            planned_end=event["end_time"],
        )
    except Exception:
        logger.warning(
            "Failed to log habit session for habit_id=%s event_id=%s",
            habit_id,
            event.get("event_id"),
            exc_info=True,
        )


async def _insert_google_event(
    access_token: str,
    calendar_id: str,
    summary: str,
    start_time: str,
    end_time: str,
    location: str | None,
    calendar_timezone: str | None,
    extended_properties: dict | None = None,
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
        if extended_properties:
            body["extendedProperties"] = {"private": extended_properties}
        item = service.events().insert(calendarId=calendar_id, body=body).execute()
        return _trim_google_event(item, calendar_id)

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
) -> tuple[dict, str | None]:
    """Returns (trimmed event, tagged habit_id or None) — the tag comes
    back on every patch response regardless of what changed, since a
    partial patch still returns the full event resource."""

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
        return (
            service.events()
            .patch(calendarId=calendar_id, eventId=event_id, body=body)
            .execute()
        )

    # googleapiclient is synchronous; keep it off the event loop like every
    # other blocking call in this codebase.
    item = await asyncio.to_thread(_patch)
    return _trim_google_event(item, calendar_id), _extract_habit_id(item)


async def _delete_google_event(access_token: str, calendar_id: str, event_id: str) -> None:
    def _delete() -> None:
        creds = Credentials(token=access_token)
        service = build("calendar", "v3", credentials=creds)
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()

    # googleapiclient is synchronous; keep it off the event loop like every
    # other blocking call in this codebase.
    await asyncio.to_thread(_delete)


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

        items: list[dict] = []
        page_token: str | None = None
        pages = 0
        while True:
            list_kwargs = dict(
                calendarId=calendar_id,
                timeMin=f"{date_from}T00:00:00Z",
                timeMax=f"{date_to}T00:00:00Z",
                singleEvents=True,
                orderBy="startTime",
            )
            if page_token:
                list_kwargs["pageToken"] = page_token
            response = service.events().list(**list_kwargs).execute()

            for item in response.get("items", []):
                start = item.get("start", {})
                end = item.get("end", {})
                entry = {
                    "event_id": item.get("id"),
                    "title": item.get("summary", "(no title)"),
                    "start_time": start.get("dateTime", start.get("date")),
                    "end_time": end.get("dateTime", end.get("date")),
                    "location": item.get("location"),
                    "calendar_id": calendar_id,
                }
                # Only present when this event was tagged for a habit — see
                # _trim_google_event's identical convention. This is what lets
                # the agent notice, from a plain get_calendar_events call,
                # that an already-scheduled event is a habit session, e.g.
                # when checking a newly-stated preference against what's
                # already on the calendar (see instruction.md).
                habit_id = _extract_habit_id(item)
                if habit_id:
                    entry["habit_id"] = habit_id
                items.append(entry)

            pages += 1
            page_token = response.get("nextPageToken")
            if not page_token:
                break
            if pages >= _MAX_EVENT_PAGES or len(items) >= _MAX_EVENTS:
                # Not raised as an error: partial results from a calendar
                # this busy are still more useful to the caller than none.
                logger.warning(
                    "calendar events pagination hit safety cap; "
                    "returning partial results",
                    extra={"page_count": pages, "event_count": len(items)},
                )
                break
        return items

    # googleapiclient is synchronous; keep it off the event loop like every
    # other blocking call in this codebase.
    return await asyncio.to_thread(_list)
