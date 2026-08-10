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

from google.adk.tools import ToolContext

from . import backend_client


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
