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
import base64
import hashlib
import logging
import random
import re
import secrets

from google.adk.tools import ToolContext
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import backend_client
from . import domain_client

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

# --- Insert idempotency + retry (A2.3) --------------------------------
#
# Every insert now carries a caller-supplied event id, which is what makes
# retrying an insert safe at all — a retried request either creates the
# event or lands a 409 "duplicate" on an id that's already there, never a
# second event. Two different ids are derived depending on what's being
# created, because "the same logical write, retried" and "the same
# content, submitted twice on purpose" need different answers:
#
# - Habit-tagged (habit_id is set): the id is a stable hash of
#   (user_id, habit_id, calendar_id, planned_start) — that tuple *is* the
#   identity of "this habit's session at this time". Retried or re-issued
#   (e.g. the model calls add_calendar_event again for a session it
#   already placed), it lands on the same event rather than duplicating
#   it. This is what "duplicate gym session" in the roadmap refers to.
# - Plain appointment (no habit_id): there's no such identity — a second
#   "drinks with friends" at the same time on the same calendar is a
#   perfectly legitimate second event, not a duplicate. Deriving its id
#   from content would silently swallow that. Instead the id is random,
#   generated once per add_calendar_event call and reused across that
#   call's own retries only, so a retried attempt still lands on the same
#   event without two distinct user-requested appointments ever colliding.
#
# Character set is Calendar's own constraint, not a stylistic choice: per
# https://developers.google.com/workspace/calendar/v3/reference/events/insert,
# a caller-supplied id must use base32hex's alphabet (lowercase a-v, 0-9),
# 5-1024 characters.
_B32_TO_B32HEX = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567",
    "0123456789ABCDEFGHIJKLMNOPQRSTUV",
)


def _base32hex(raw: bytes) -> str:
    b32 = base64.b32encode(raw).decode().rstrip("=")
    return b32.translate(_B32_TO_B32HEX).lower()


def _habit_session_event_id(
    user_id: str, habit_id: str, calendar_id: str, planned_start: str
) -> str:
    raw = f"{user_id}:{habit_id}:{calendar_id}:{planned_start}".encode()
    return _base32hex(hashlib.sha256(raw).digest())


def _fresh_event_id() -> str:
    """160 bits of randomness, base32hex-encoded — collision odds are
    negligible, unlike a content hash of a plain appointment."""
    return _base32hex(secrets.token_bytes(20))


# 429/5xx are the transient classes worth retrying; everything else (400,
# 401 — handled separately by backend_client, not this API — 403, 404) is
# either a real client error or something a retry can't fix.
_RETRYABLE_INSERT_STATUSES = frozenset({429, 500, 502, 503, 504})

# Bounded so one tool call can't run away inside a single turn (the app
# service's own request timeout is 900s — see A0's prerequisite — and this
# is one insert among dozens of calls in a planning turn). Three retries
# past the initial attempt is enough to ride out a brief blip without
# eating a meaningful fraction of that budget even at the capped delay.
_MAX_INSERT_ATTEMPTS = 4
_INSERT_RETRY_BASE_DELAY_S = 0.5
_INSERT_RETRY_MAX_DELAY_S = 8.0

# Module attribute rather than a bare asyncio.sleep call so tests can swap
# it for a no-op and not actually wait out the backoff — same pattern as
# agent.py's _now() (A0.4).
_sleep = asyncio.sleep


async def _sleep_before_retry(attempt: int) -> None:
    # Full jitter: a random delay in [0, cap], where cap grows
    # exponentially with attempt number. Spreads out retries from
    # concurrent callers instead of having them all thunder back at
    # once on the same schedule.
    cap = min(_INSERT_RETRY_MAX_DELAY_S, _INSERT_RETRY_BASE_DELAY_S * (2 ** (attempt - 1)))
    await _sleep(random.uniform(0, cap))


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
    except backend_client.BACKEND_ERROR:
        logger.warning("get_calendar_events: list_calendars backend call failed", exc_info=True)
        return {
            "status": "error",
            "error_message": (
                "Could not check connected calendars right now due to a "
                "backend error — this does not mean nothing is connected."
            ),
        }

    # Sorted for reproducibility only — the original dict comprehension
    # this replaces iterated a set too, so there was never an ordering
    # guarantee to preserve here, unlike events.sort below.
    account_ids = sorted({c["account_id"] for c in calendars["calendars"]})
    try:
        tokens = await asyncio.gather(
            *(backend_client.access_token(user_id, account_id) for account_id in account_ids)
        )
    except backend_client.BACKEND_ERROR:
        logger.warning("get_calendar_events: access_token backend call failed", exc_info=True)
        return {
            "status": "error",
            "error_message": "Could not verify calendar access right now due to a backend error.",
        }
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


async def resolve_reference_timezone(tool_context: ToolContext, user_id: str) -> str | None:
    """The IANA timezone to interpret zones/sleep-schedule wall-clock
    times in — used by scheduling_tool.py's get_available_slots (A4.2).

    Nothing in this codebase's data model carries a per-user timezone of
    its own; zones and the sleep schedule store bare "HH:MM" strings (see
    A6.3's zones.py docstring for that gap). The least surprising stand-in
    is the timezone of the user's own primary calendar — the same
    calendar add_calendar_event defaults to when calendar_summary is
    omitted (see _candidate_calendars), so a habit session the engine
    proposes and one the model places by hand land in the same frame of
    reference.

    Returns None if no connected calendar's timezone could be read at all
    (nothing to resolve a reference from — the caller decides what that
    means for its own response). NeedsAuth/BACKEND_ERROR from the
    underlying calendar-list/token calls are not caught here; they
    propagate to the caller like every other calendar_tool function.
    """
    calendars = await _cached_list_calendars(tool_context, user_id)
    for candidate in _candidate_calendars(calendars["calendars"], None):
        token = await backend_client.access_token(user_id, candidate["account_id"])
        if token is None:
            continue
        try:
            entry = await _cached_calendar_list_entry(
                tool_context, candidate["account_id"], token, candidate["calendar_id"]
            )
        except HttpError:
            continue
        tz_name = entry.get("timeZone")
        if tz_name:
            return tz_name
    return None


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
        id, title, times, and a link, and "retry_count" is present (>0)
        only if a transient failure had to be retried before it succeeded.
        On "needs_auth", "connect_url" is a link to hand the user. On
        "not_found", no connected calendar matched calendar_summary. On
        "not_writable", the matched calendar(s) are read-only for this user
        (e.g. a subscribed holiday calendar, or a calendar someone else
        shared without edit access) — tell the user rather than guessing at
        a substitute.
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
    except backend_client.BACKEND_ERROR:
        logger.warning("add_calendar_event: list_calendars backend call failed", exc_info=True)
        return {
            "status": "error",
            "error_message": (
                "Could not check connected calendars right now due to a "
                "backend error — this does not mean nothing is connected."
            ),
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
        try:
            token = await backend_client.access_token(user_id, candidate["account_id"])
        except backend_client.BACKEND_ERROR:
            logger.warning("add_calendar_event: access_token backend call failed", exc_info=True)
            return {
                "status": "error",
                "error_message": "Could not verify calendar access right now due to a backend error.",
            }
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

    event_id = (
        _habit_session_event_id(user_id, habit_id, target["calendar_id"], start_time)
        if habit_id
        else _fresh_event_id()
    )
    try:
        event, retry_count = await _insert_google_event_with_retry(
            token,
            target["calendar_id"],
            summary,
            start_time,
            end_time,
            location,
            entry.get("timeZone"),
            event_id=event_id,
            extended_properties={_HABIT_ID_PROPERTY: habit_id} if habit_id else None,
        )
    except HttpError as exc:
        return {"status": "error", "error_message": str(exc)}

    if habit_id:
        await _log_habit_session(user_id, habit_id, event)

    result: dict = {"status": "success", "event": event}
    if retry_count:
        result["retry_count"] = retry_count
    return result


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
    except backend_client.BACKEND_ERROR:
        logger.warning("list_calendars backend call failed", exc_info=True)
        return {
            "status": "error",
            "error_message": (
                "Could not check connected calendars right now due to a "
                "backend error — this does not mean nothing is connected."
            ),
        }

    candidate = next(
        (c for c in calendars["calendars"] if c["calendar_id"] == calendar_id), None
    )
    if candidate is None:
        return {
            "status": "not_found",
            "message": f"No connected calendar with id {calendar_id!r}.",
        }

    try:
        token = await backend_client.access_token(user_id, candidate["account_id"])
    except backend_client.BACKEND_ERROR:
        logger.warning("access_token backend call failed", exc_info=True)
        return {
            "status": "error",
            "error_message": "Could not verify calendar access right now due to a backend error.",
        }
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
    except backend_client.BACKEND_ERROR:
        logger.warning("list_calendars backend call failed", exc_info=True)
        return {
            "status": "error",
            "error_message": (
                "Could not check connected calendars right now due to a "
                "backend error — this does not mean nothing is connected."
            ),
        }

    candidate = next(
        (c for c in calendars["calendars"] if c["calendar_id"] == calendar_id), None
    )
    if candidate is None:
        return {
            "status": "not_found",
            "message": f"No connected calendar with id {calendar_id!r}.",
        }

    try:
        token = await backend_client.access_token(user_id, candidate["account_id"])
    except backend_client.BACKEND_ERROR:
        logger.warning("access_token backend call failed", exc_info=True)
        return {
            "status": "error",
            "error_message": "Could not verify calendar access right now due to a backend error.",
        }
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
    invisible to a future review_habit_week, not a broken calendar.

    Goes through domain_client (day_planner_backend_app's /agent/*, A6.2),
    not backend_client — habit sessions moved there in A6.1, unlike the
    calendar-credential calls the rest of this file makes."""
    try:
        await domain_client.upsert_habit_session(
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
    event_id: str | None = None,
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
        if event_id:
            body["id"] = event_id
        if location:
            body["location"] = location
        if extended_properties:
            body["extendedProperties"] = {"private": extended_properties}
        item = service.events().insert(calendarId=calendar_id, body=body).execute()
        return _trim_google_event(item, calendar_id)

    # googleapiclient is synchronous; keep it off the event loop like every
    # other blocking call in this codebase.
    return await asyncio.to_thread(_insert)


async def _fetch_google_event_or_none(
    access_token: str, calendar_id: str, event_id: str
) -> dict | None:
    """The raw (untrimmed) event resource, or None if it doesn't exist.
    Used only to resolve a 409 on insert — needs the raw "status" field
    (confirmed/cancelled), which _trim_google_event doesn't carry."""

    def _get() -> dict | None:
        creds = Credentials(token=access_token)
        service = build("calendar", "v3", credentials=creds)
        try:
            return service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        except HttpError as exc:
            if exc.resp.status == 404:
                return None
            raise

    return await asyncio.to_thread(_get)


async def _insert_google_event_with_retry(
    access_token: str,
    calendar_id: str,
    summary: str,
    start_time: str,
    end_time: str,
    location: str | None,
    calendar_timezone: str | None,
    *,
    event_id: str,
    extended_properties: dict | None = None,
) -> tuple[dict, int]:
    """Returns (trimmed event, retry_count). event_id is always supplied
    by the caller (see the module-level note on _habit_session_event_id /
    _fresh_event_id) — that's what makes retrying safe at all.

    A 409 means this id already exists on the calendar. That's not
    unconditionally success: Google retains a deleted event's id as a
    tombstone, so re-placing a habit session at a slot it was once
    (and no longer is) booked at can 409 against a cancelled event that
    isn't really there. The existing event is fetched and checked — live
    means a prior attempt already landed and this is a genuine idempotent
    replay; cancelled or gone means the id is unusable for a different
    reason, and a fresh one is minted and retried rather than reporting
    success over an event that doesn't exist.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            event = await _insert_google_event(
                access_token,
                calendar_id,
                summary,
                start_time,
                end_time,
                location,
                calendar_timezone,
                event_id=event_id,
                extended_properties=extended_properties,
            )
            return event, attempt - 1
        except HttpError as exc:
            status = exc.resp.status
            if status == 409:
                existing = await _fetch_google_event_or_none(access_token, calendar_id, event_id)
                if existing is not None and existing.get("status") != "cancelled":
                    return _trim_google_event(existing, calendar_id), attempt - 1
                event_id = _fresh_event_id()
                if attempt >= _MAX_INSERT_ATTEMPTS:
                    raise
            elif status not in _RETRYABLE_INSERT_STATUSES or attempt >= _MAX_INSERT_ATTEMPTS:
                raise
        except OSError:
            if attempt >= _MAX_INSERT_ATTEMPTS:
                raise
        await _sleep_before_retry(attempt)


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
