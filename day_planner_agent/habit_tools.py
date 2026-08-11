"""Structured habit tracking for the day planner agent.

Habits are recurring goals the agent proactively schedules onto the
calendar (see instruction.md) — "180 min/week of exercise, sessions 30-60
min", "read 20-40 minutes most nights". They used to live only as sentences
inside the free-text preference profile (memory_tools.py's update_profile),
which gave every habit no stable identity: nothing to tag a calendar event
with later, nothing to query deterministically, and no guarantee Memory
Bank's own LLM-driven merge preserved exact wording across updates.

Habit records instead live in day_planner_backend_internal's Firestore, the
same deterministic store already used for calendar-account identity (see
../docs/oauth-design.md §7 for why credential/identity-adjacent state
doesn't belong in Memory Bank) — every habit gets a stable habit_id the
rest of the system can rely on.

user_id always comes from tool_context.session.user_id, the same rule as
every other tool in this codebase — never a model-supplied argument.
"""

from __future__ import annotations

from datetime import datetime

from google.adk.tools import ToolContext

from . import backend_client
from . import calendar_tool


async def create_habit(tool_context: ToolContext, label: str, goal: str) -> dict:
    """Start tracking a new habit — a recurring goal the agent should
    proactively schedule onto the calendar (see instruction.md's guidance
    on placing sessions for a habit over a period).

    Call this the first time the user states a new standing goal like this
    (e.g. "I want to exercise 180 minutes a week, sessions 30-60 min" or
    "read 20-40 minutes most nights") — not for a single day's plan, which
    belongs on the calendar via add_calendar_event instead, and not for a
    fixed preference/constraint with no target of its own (e.g. "no
    physical activity after 8pm", gym timing, sleep schedule, work hours)
    — those still go through update_profile, even if phrased like a goal.

    If one message states several distinct goals (e.g. "I want to
    maintain 90 minutes of gym and 1 hour of tennis per week"), call this
    once per goal — each gets its own label and id (Gym, Tennis), since
    they're tracked and scheduled independently.

    Args:
        label: A short name for the habit, e.g. "Gym", "Meditation",
            "Reading".
        goal: The goal itself, in your own words, including frequency and
            session-length range, e.g. "180 minutes/week, sessions 30-60
            minutes, mornings preferred".

    Returns:
        dict with "status" ("success" or "error") and, on success, "habit"
        (habit_id, label, goal, status, created_at, updated_at).
    """
    habit = await backend_client.create_habit(
        tool_context.session.user_id, label=label, goal=goal
    )
    return {"status": "success", "habit": habit}


async def list_habits(tool_context: ToolContext, include_inactive: bool = False) -> dict:
    """List the user's tracked habits.

    Call this — not get_profile — when you need to know what recurring
    goals to place on the calendar; habits live here, not in the free-text
    profile, and aren't preloaded at session start the way the profile is.

    Args:
        include_inactive: If True, also return paused and archived habits.
            Defaults to False (active habits only), which is almost always
            what you want for day-to-day planning.

    Returns:
        dict with "status" and "habits" (a list of habit_id, label, goal,
        status, created_at, updated_at — empty if none exist yet).
    """
    habits = await backend_client.list_habits(
        tool_context.session.user_id, status=None if include_inactive else "active"
    )
    return {"status": "success", "habits": habits}


async def update_habit(
    tool_context: ToolContext,
    habit_id: str,
    label: str | None = None,
    goal: str | None = None,
    status: str | None = None,
) -> dict:
    """Update or retire an existing habit.

    habit_id must come from a prior list_habits or create_habit result —
    never guess it or ask the user for it; call list_habits first if you
    don't already have it from earlier in the conversation.

    Only pass the fields that are actually changing. To retire a habit the
    user no longer wants tracked, set status="archived" rather than trying
    to delete it — that keeps its history intact for anything that
    referenced it. Use status="paused" instead for a temporary break (e.g.
    "skip gym while I'm traveling this month") the user is likely to
    resume.

    Args:
        habit_id: The habit's id, from a prior list_habits or create_habit
            result.
        label: New short name, if it's changing.
        goal: New goal description, if it's changing.
        status: New status — "active", "paused", or "archived".

    Returns:
        dict with "status" ("success", "not_found", or "error") and, on
        success, "habit" (the updated record).
    """
    if not any([label, goal, status]):
        return {"status": "error", "message": "No fields provided to update."}

    updated = await backend_client.update_habit(
        tool_context.session.user_id, habit_id, label=label, goal=goal, status=status
    )
    if updated is None:
        return {"status": "not_found", "message": f"No habit {habit_id!r}."}
    return {"status": "success", "habit": updated}


async def review_habit_week(tool_context: ToolContext, date_from: str, date_to: str) -> dict:
    """Compare the habit sessions the agent planned for a period against
    what actually happened on the calendar — the "why do I keep missing
    gym on Thursdays" feature. Only covers sessions add_calendar_event or
    update_calendar_event tagged with a habit_id; a user-created event with
    no habit tag never shows up here.

    Call this when the user asks how their week/habits went, whether
    they're keeping up with something, or why a habit keeps failing —
    never guess at an answer to that kind of question from memory, this is
    the tool that actually knows. It's also worth calling proactively
    before placing next period's sessions for a habit that had a rough
    prior period, so the new plan can route around whatever kept bumping
    it (see instruction.md's guidance on using this alongside the user's
    stated preferences to judge what the pattern actually means).

    Args:
        date_from: Start date, inclusive, "YYYY-MM-DD" — same contract as
            get_calendar_events.
        date_to: End date, exclusive, "YYYY-MM-DD".

    Returns:
        A dict with "status". On "success", "sessions" is a list of
        planned habit sessions in the period, each with "habit_id",
        "habit_label", "planned_start", "planned_end", "event_id",
        "calendar_id", and "outcome" — "kept" (still there, unchanged),
        "moved" (still there, different time — could be a deliberate
        reschedule or a conflict, "bumped_by" is the best signal for
        which), or "gone" (deleted). Anything not "kept" also carries
        "bumped_by": the title of whatever else now occupies that original
        slot, or null if nothing does (the session was likely just
        dropped, not displaced). Empty "sessions" means no habit session
        was planned in this period at all — distinct from every one of
        them being kept, so don't conflate the two when summarizing. On
        "needs_auth"/"error", handle identically to get_calendar_events.
    """
    user_id = tool_context.session.user_id

    sessions = await backend_client.list_habit_sessions(
        user_id, planned_from=f"{date_from}T00:00:00Z", planned_to=f"{date_to}T00:00:00Z"
    )
    if not sessions:
        return {"status": "success", "sessions": []}

    calendar_state = await calendar_tool.get_calendar_events(tool_context, date_from, date_to)
    if calendar_state["status"] != "success":
        return calendar_state

    events = calendar_state["events"]
    events_by_key = {(e["calendar_id"], e["event_id"]): e for e in events}

    habits = await backend_client.list_habits(user_id, status=None)
    habit_labels = {h["habit_id"]: h["label"] for h in habits}

    results = []
    for session in sessions:
        current = events_by_key.get((session["calendar_id"], session["event_id"]))

        if current is None:
            outcome = "gone"
        elif _same_instant(current["start_time"], session["planned_start"]):
            outcome = "kept"
        else:
            outcome = "moved"

        entry = {
            "habit_id": session["habit_id"],
            "habit_label": habit_labels.get(session["habit_id"], session["habit_id"]),
            "event_id": session["event_id"],
            "calendar_id": session["calendar_id"],
            "planned_start": session["planned_start"],
            "planned_end": session["planned_end"],
            "outcome": outcome,
        }
        if outcome != "kept":
            entry["bumped_by"] = _find_conflict(events, session)
        results.append(entry)

    return {"status": "success", "sessions": results}


def _same_instant(a: str, b: str) -> bool:
    """Compares two Calendar API time strings as actual instants, not
    text — "2026-08-04T20:00:00-07:00" and its UTC-normalized equivalent
    must compare equal even though they're different strings."""
    try:
        return datetime.fromisoformat(a) == datetime.fromisoformat(b)
    except (ValueError, TypeError):
        return a == b


def _find_conflict(events: list[dict], session: dict) -> str | None:
    """The title of whatever now overlaps a session's originally-planned
    slot on the same calendar, excluding the session's own event — the
    best available signal for *why* a session moved or disappeared."""
    try:
        planned_start = datetime.fromisoformat(session["planned_start"])
        planned_end = datetime.fromisoformat(session["planned_end"])
    except (ValueError, TypeError):
        return None

    for event in events:
        if (
            event["calendar_id"] != session["calendar_id"]
            or event["event_id"] == session["event_id"]
        ):
            continue
        try:
            overlaps = (
                datetime.fromisoformat(event["start_time"]) < planned_end
                and datetime.fromisoformat(event["end_time"]) > planned_start
            )
        except (ValueError, TypeError):
            continue
        if overlaps:
            return event["title"]
    return None
