"""Structured day-zone tracking for the day planner agent.

Zones are named, recurring scheduling restrictions — work hours, commute,
or any other block of the week a user names — that used to live only as
free-text sentences inside the preference profile (memory_tools.py's
update_profile), the same gap habit_tools.py already closed for recurring
goals. A sentence like "I work 9-5 weekdays" gave the model no
deterministic way to check placement against it; it had to notice and
correctly re-apply the wording every single time, which is what actually
caused a habit session to get placed during work hours once (see
instruction.md's placement guidance and docs/todo.md §1).

Zone records live in day_planner_backend_internal's Firestore, the same
store habits already use and for the same reason (see habit_tools.py's
module docstring) — every zone gets a stable zone_id.

A zone is a restriction by default: no habit may be placed inside one
unless the zone's label appears in that habit's own allowed_zones (see
update_habit in habit_tools.py). That's a *standing* exception, persisted
on the habit — a one-off "just today, squeeze it into work hours" from
the user stays purely conversational and must never be written there
(see instruction.md's guardrail-override guidance).

Sleep is modeled separately (set_sleep_schedule/get_sleep_schedule, not
create_zone) because cool-down and wake-up are offsets from sleep's own
boundaries, not independent named windows — see day_planner_backend_internal's
SleepSchedule docstring for the full reasoning.

user_id always comes from tool_context.session.user_id, the same rule as
every other tool in this codebase — never a model-supplied argument.
"""

from __future__ import annotations

from google.adk.tools import ToolContext

from . import backend_client

# Session-state key agent.py's _preload_zones writes the preloaded zone
# list to. Public here (rather than private in agent.py, which is where
# it originated) so habit_tools.py can read the same cache for A1.4's
# telemetry without a circular import (agent.py already imports this
# module) or a second, redundant list_zones network call on every
# review_habit_week — zones are already sitting in session state by the
# time any tool runs, the same way agent.py's instruction-building reads
# them.
PRELOADED_ZONES_STATE_KEY = "day_planner:preloaded_zones"


async def create_zone(
    tool_context: ToolContext,
    label: str,
    start_time: str,
    end_time: str,
    days_of_week: list[str],
) -> dict:
    """Start tracking a new named scheduling restriction — work hours,
    commute, or any other recurring block of the week the user describes.

    Call this the first time the user states a standing restriction like
    this (e.g. "I work 9 to 5 on weekdays" or "I commute 8-9am Monday to
    Friday") — not for a single day's plan (that's add_calendar_event
    instead), and not for a recurring *goal* with its own target (that's
    create_habit — a zone has no target of its own, only a window).

    By default a zone rules out every habit from being placed inside it.
    If the user says a specific habit is fine during this window (e.g. "a
    quick workout during my lunch break at work is fine"), don't encode
    that here — call update_habit on that habit with allowed_zones
    instead, naming this zone's label. If they're only making a one-off
    exception for a single day ("just today, squeeze my workout into work
    hours"), don't call any tool for that at all — handle it purely in
    conversation for that occurrence, per instruction.md's guardrail
    guidance.

    Args:
        label: A short, stable name for the zone, e.g. "Work", "Commute".
            This is what a habit's allowed_zones references, so keep it
            consistent if you create related habits later.
        start_time: 24-hour "HH:MM" wall-clock time the zone begins, e.g.
            "09:00".
        end_time: 24-hour "HH:MM" wall-clock time the zone ends, e.g.
            "17:00".
        days_of_week: Which days this zone applies to, from "mon", "tue",
            "wed", "thu", "fri", "sat", "sun". A weekday-only zone simply
            doesn't restrict weekends — no need to ask about weekends
            separately.

    Returns:
        dict with "status" ("success" or "error") and, on success, "zone"
        (zone_id, label, start_time, end_time, days_of_week, created_at,
        updated_at).
    """
    zone = await backend_client.create_zone(
        tool_context.session.user_id,
        label=label,
        start_time=start_time,
        end_time=end_time,
        days_of_week=days_of_week,
    )
    return {"status": "success", "zone": zone}


async def list_zones(tool_context: ToolContext) -> dict:
    """List every scheduling-restriction zone the user has defined (work
    hours, commute, etc.) — not sleep, which is its own thing; see
    get_sleep_schedule.

    Call this before placing or adjusting a habit session so you know
    what windows are off-limits by default. Zones are low-cardinality and
    change rarely, so they're preloaded once at session start the same
    way the profile is — this is here for a mid-conversation re-check
    after the user adds or changes one, since the preloaded snapshot goes
    stale the moment that happens.

    Returns:
        dict with "status" and "zones" (a list of zone_id, label,
        start_time, end_time, days_of_week, created_at, updated_at —
        empty if the user has none, which just means no restriction of
        this kind exists for them).
    """
    zones = await backend_client.list_zones(tool_context.session.user_id)
    return {"status": "success", "zones": zones}


async def update_zone(
    tool_context: ToolContext,
    zone_id: str,
    label: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    days_of_week: list[str] | None = None,
) -> dict:
    """Update an existing zone (e.g. work hours changed).

    zone_id must come from a prior list_zones or create_zone result —
    never guess it or ask the user for it; call list_zones first if you
    don't already have it from earlier in the conversation. Only pass the
    fields that are actually changing.

    There is no delete — a zone the user no longer wants isn't a case
    that's come up; if it does, treat it as an update to the window
    itself rather than trying to remove the record.

    Args:
        zone_id: The zone's id, from a prior list_zones or create_zone
            result.
        label: New name, if it's changing. Update any habit's
            allowed_zones that referenced the old label if you rename
            one — they won't be renamed automatically.
        start_time: New 24-hour "HH:MM" start time, if it's changing.
        end_time: New 24-hour "HH:MM" end time, if it's changing.
        days_of_week: New full list of days this zone applies to, if
            it's changing — replaces the stored list, not a merge.

    Returns:
        dict with "status" ("success", "not_found", or "error") and, on
        success, "zone" (the updated record).
    """
    if not any([label, start_time, end_time, days_of_week]):
        return {"status": "error", "message": "No fields provided to update."}

    updated = await backend_client.update_zone(
        tool_context.session.user_id,
        zone_id,
        label=label,
        start_time=start_time,
        end_time=end_time,
        days_of_week=days_of_week,
    )
    if updated is None:
        return {"status": "not_found", "message": f"No zone {zone_id!r}."}
    return {"status": "success", "zone": updated}


async def get_sleep_schedule(tool_context: ToolContext) -> dict:
    """Fetch the user's sleep schedule, if they've ever set one.

    Sleep is preloaded once at session start the same way the profile
    is; call this instead when you need the current values mid-
    conversation — after calling set_sleep_schedule yourself, or if
    nothing was on file at session start but the user has since set one.

    Returns:
        dict with "status" and "exists" (False if the user has never set
        a sleep schedule — every derived window is simply not a
        constraint yet, not an error). When "exists" is True, "schedule"
        holds sleep_time, wake_time (both 24-hour "HH:MM", the default
        applied to every day), "day_overrides" (a dict of day code ->
        {"sleep_time"?, "wake_time"?} for days that differ from the
        default, e.g. sleeping in on Sundays), "cool_down_minutes" (the
        wind-down window immediately before sleep_time), and
        "wake_up_buffer_minutes" (the grace window immediately after
        wake_time).
    """
    schedule = await backend_client.get_sleep_schedule(tool_context.session.user_id)
    if schedule is None:
        return {"status": "success", "exists": False}
    return {"status": "success", "exists": True, "schedule": schedule}


async def set_sleep_schedule(
    tool_context: ToolContext,
    sleep_time: str | None = None,
    wake_time: str | None = None,
    cool_down_minutes: int | None = None,
    wake_up_buffer_minutes: int | None = None,
    day_overrides: dict[str, dict[str, str]] | None = None,
) -> dict:
    """Set or update the user's sleep schedule — the special, singular
    zone cool-down and wake-up are derived from (see create_zone; sleep
    isn't a plain zone).

    Call this the first time the user states their sleep/wake times, or
    when any part of it changes (a new bedtime, a new cool-down window,
    adding a day that differs from the rest). The first call creates it;
    later calls update only the fields you pass — call get_sleep_schedule
    first if you're changing just one thing and want to confirm the rest
    stays what you expect.

    Args:
        sleep_time: 24-hour "HH:MM" bedtime applied to every day by
            default, e.g. "23:00". Required the first time this is set.
        wake_time: 24-hour "HH:MM" wake time applied to every day by
            default, e.g. "07:00". Required the first time this is set.
        cool_down_minutes: Minutes immediately before sleep_time to treat
            as wind-down, off-limits the same way a zone is — e.g. 30
            means the 30 minutes before bed. Required the first time.
        wake_up_buffer_minutes: Minutes immediately after wake_time to
            treat as a grace period, off-limits the same way — e.g. 15.
            Required the first time.
        day_overrides: Only for days that differ from sleep_time/
            wake_time above, e.g. {"sun": {"wake_time": "09:00"}} for
            sleeping in on Sundays. Replaces the whole stored map when
            you pass it — include every override that should still
            apply, not just the one changing, and pass {} to clear all
            of them. Omit entirely to leave existing overrides as they
            are.

    Returns:
        dict with "status" ("success" or "error") and, on success,
        "schedule" (the full updated record, same shape as
        get_sleep_schedule's).
    """
    # Checked against None explicitly, not truthiness: cool_down_minutes=0
    # and wake_up_buffer_minutes=0 are meaningful values ("no buffer at
    # all"), not the same as "not provided".
    if all(
        field is None
        for field in (sleep_time, wake_time, cool_down_minutes, wake_up_buffer_minutes, day_overrides)
    ):
        return {"status": "error", "message": "No fields provided to update."}

    schedule = await backend_client.set_sleep_schedule(
        tool_context.session.user_id,
        sleep_time=sleep_time,
        wake_time=wake_time,
        cool_down_minutes=cool_down_minutes,
        wake_up_buffer_minutes=wake_up_buffer_minutes,
        day_overrides=day_overrides,
    )
    return {"status": "success", "schedule": schedule}
