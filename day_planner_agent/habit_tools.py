"""Structured habit tracking for the day planner agent.

Habits are recurring goals the agent proactively schedules onto the
calendar (see instruction.md) — "180 min/week of exercise, sessions 30-60
min", "read 20-40 minutes most nights". They used to live only as sentences
inside the free-text preference profile (memory_tools.py's update_profile),
which gave every habit no stable identity: nothing to tag a calendar event
with later, nothing to query deterministically, and no guarantee Memory
Bank's own LLM-driven merge preserved exact wording across updates.

Habit records instead live in Firestore — every habit gets a stable
habit_id the rest of the system can rely on. Owned by
day_planner_backend_app as of A6.1 (it used to live behind
day_planner_backend_internal, the credential-only service — see
../docs/oauth-design.md §7 for why credential/identity-adjacent state
belongs there and ordinary planning data doesn't); reached here through
domain_client.py's OIDC-authenticated calls to its /agent/* routes (A6.2).

user_id always comes from tool_context.session.user_id, the same rule as
every other tool in this codebase — never a model-supplied argument.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from google.adk.tools import ToolContext

from . import domain_client
from . import calendar_tool
from . import zone_tools

logger = logging.getLogger(__name__)

# Mirrors turn_log.py's identical constant in day_planner_backend_app —
# see review_habit_week's telemetry block below and turn_log.TurnRecorder
# for the two ends of this channel. A tool_context.state write (as
# opposed to something in the return value) surfaces in the ADK event's
# actions.state_delta, which turn_log.py already scans for A0.2's
# preload_ok the same way — this is what lets per-session outcome
# telemetry reach Cloud Logging (A1.4) without adding a single token to
# what review_habit_week returns to the model (A1.5's return shape is
# unchanged).
_HABIT_SESSION_OUTCOMES_STATE_KEY = "day_planner:habit_session_outcomes"

_WEEKDAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _hash_session_ref(calendar_id: str, event_id: str) -> str:
    """One-way session identifier for telemetry dedup — see
    _emit_habit_session_telemetry. Deliberately not the raw
    habit_session_id_for(calendar_id, event_id) string: calendar_id for a
    user's primary Google calendar *is* their email address, and A1.1's
    redaction rule exists specifically to keep that kind of value out of
    Cloud Logging."""
    return hashlib.sha256(f"{calendar_id}__{event_id}".encode()).hexdigest()[:16]


async def create_habit(tool_context: ToolContext, label: str, goal: str) -> dict:
    """Start tracking a new habit — a recurring goal the agent should
    proactively schedule onto the calendar (see instruction.md's guidance
    on placing sessions for a habit over a period).

    Call this the first time the user states a new standing goal like this
    (e.g. "I want to exercise 180 minutes a week, sessions 30-60 min" or
    "read 20-40 minutes most nights") — not for a single day's plan, which
    belongs on the calendar via add_calendar_event instead, and not for a
    fixed preference/constraint with no target of its own and no
    independent calendar footprint (e.g. "no physical activity after
    8pm", gym timing, sleep schedule, work hours) — those still go
    through update_profile, even if phrased like a goal. A habit that
    rides entirely on an existing zone's own occurrences instead (e.g.
    "listen to an audiobook whenever I have commute") still belongs here,
    not update_profile, even though it has no numeric target either — see
    the goal Arg below, and set allowed_zones via update_habit afterward.

    If one message states several distinct goals (e.g. "I want to
    maintain 90 minutes of gym and 1 hour of tennis per week"), call this
    once per goal — each gets its own label and id (Gym, Tennis), since
    they're tracked and scheduled independently.

    Args:
        label: A short name for the habit, e.g. "Gym", "Meditation",
            "Reading".
        goal: The goal itself, in your own words, including frequency and
            session-length range, e.g. "180 minutes/week, sessions 30-60
            minutes, mornings preferred" — or, for a habit anchored to a
            zone instead (see above), just what the activity is, e.g.
            "listen to an audiobook"; it needs no frequency/length of its
            own since the zone's occurrences are the target. Don't ask
            the user for one in that case.

    Returns:
        dict with "status" ("success" or "error") and, on success, "habit"
        (habit_id, label, goal, status, created_at, updated_at). On
        "error", the habit was not created — a backend failure, not a
        validation problem; retry rather than assuming it was rejected.
    """
    try:
        habit = await domain_client.create_habit(
            tool_context.session.user_id, label=label, goal=goal
        )
    except domain_client.BACKEND_ERROR:
        logger.warning("create_habit backend call failed", exc_info=True)
        return {
            "status": "error",
            "message": "Could not create the habit right now due to a backend error. Try again shortly.",
        }
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
        status, created_at, updated_at — empty if none exist yet). On
        "error", habits could not be loaded due to a backend failure —
        this is not the same as the user having none tracked; do not
        place any habit session on the strength of this call, and do not
        tell the user they have no habits.
    """
    try:
        habits = await domain_client.list_habits(
            tool_context.session.user_id, status=None if include_inactive else "active"
        )
    except domain_client.BACKEND_ERROR:
        logger.warning("list_habits backend call failed", exc_info=True)
        return {
            "status": "error",
            "message": (
                "Could not load habits right now due to a backend error — this "
                "does not mean the user has none tracked. Retry before assuming "
                "there is nothing to place."
            ),
        }
    return {"status": "success", "habits": habits}


async def update_habit(
    tool_context: ToolContext,
    habit_id: str,
    label: str | None = None,
    goal: str | None = None,
    status: str | None = None,
    allowed_zones: list[str] | None = None,
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
        allowed_zones: The full list of zone labels (see zone_tools.py's
            create_zone) this habit may be placed in, in addition to any
            unzoned (open) time — e.g. ["Work"] for a habit explicitly
            allowed during work hours. Also accepts the fixed labels
            "cool-down" and "wake-up" for the two overridable windows
            derived from the sleep schedule (never the sleep period
            itself — that has no override). Replaces the stored list
            wholesale when provided, not a merge — pass every zone that
            should still apply, not just a new one; pass [] to clear it.
            Only for a *standing* exception the user states as a rule for
            this habit going forward; a one-off "just today" exception
            stays purely conversational and must never be written here.

    Returns:
        dict with "status" ("success", "not_found", or "error") and, on
        success, "habit" (the updated record). On "error", a backend
        failure prevented the update — not a validation problem.
    """
    if not any([label, goal, status, allowed_zones is not None]):
        return {"status": "error", "message": "No fields provided to update."}

    try:
        updated = await domain_client.update_habit(
            tool_context.session.user_id,
            habit_id,
            label=label,
            goal=goal,
            status=status,
            allowed_zones=allowed_zones,
        )
    except domain_client.BACKEND_ERROR:
        logger.warning("update_habit backend call failed", exc_info=True)
        return {
            "status": "error",
            "message": "Could not update the habit right now due to a backend error. Try again shortly.",
        }
    if updated is None:
        return {"status": "not_found", "message": f"No habit {habit_id!r}."}
    return {"status": "success", "habit": updated}


async def review_habit_week(tool_context: ToolContext, date_from: str, date_to: str) -> dict:
    """Compare the habit sessions the agent planned for a period against
    what actually happened — both whether the user actually did each one
    (status) and what happened to it on the calendar (outcome). Only
    covers sessions add_calendar_event or update_calendar_event tagged
    with a habit_id; a user-created event with no habit tag never shows up
    here.

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
        "calendar_id", "session_status", and "outcome".

        "session_status" is the answer to *whether* it happened —
        "completed", "skipped", or "pending" (nobody has said either way
        yet; this is not itself a failure, never report it as one). Set
        only via mark_habit_session or the user marking it themselves,
        never inferred from calendar state. "completed_at"/"marked_by" are
        set once "session_status" is "completed" or "skipped", else null.

        "outcome" is the calendar diff, answering *why* — "kept" (still
        there, unchanged), "moved" (still there, different time — could be
        a deliberate reschedule or a conflict, "bumped_by" is the best
        signal for which), or "gone" (deleted). Anything not "kept" also
        carries "bumped_by": the title of whatever else now occupies that
        original slot, or null if nothing does.

        Read the two together, don't collapse them: "moved" + "completed"
        is a successful reschedule, not a partial failure — report it as a
        success. "gone" + "pending" is a session that was dropped with no
        signal either way; "gone" + "completed" means the user did it and
        then cleaned up the calendar entry, still a success. Only
        "pending" sessions whose planned time has passed are worth
        surfacing as open questions ("did you get to gym Tuesday?") —
        never state or imply they didn't happen.

        Empty "sessions" means no habit session was planned in this period
        at all — distinct from every one of them being kept/completed, so
        don't conflate the two when summarizing. On "needs_auth"/"error",
        handle identically to get_calendar_events — an "error" here means
        the review could not be performed due to a backend failure, not
        that nothing was planned; don't report it as an empty period.
    """
    user_id = tool_context.session.user_id

    try:
        sessions = await domain_client.list_habit_sessions(
            user_id, planned_from=f"{date_from}T00:00:00Z", planned_to=f"{date_to}T00:00:00Z"
        )
    except domain_client.BACKEND_ERROR:
        logger.warning("review_habit_week: list_habit_sessions backend call failed", exc_info=True)
        return {
            "status": "error",
            "message": (
                "Could not check habit session history right now due to a "
                "backend error — this does not mean nothing was planned. "
                "Try again shortly rather than reporting an empty period."
            ),
        }
    if not sessions:
        return {"status": "success", "sessions": []}

    calendar_state = await calendar_tool.get_calendar_events(tool_context, date_from, date_to)
    if calendar_state["status"] != "success":
        return calendar_state

    events = calendar_state["events"]
    events_by_key = {(e["calendar_id"], e["event_id"]): e for e in events}

    try:
        habits = await domain_client.list_habits(user_id, status=None)
        habit_labels = {h["habit_id"]: h["label"] for h in habits}
    except domain_client.BACKEND_ERROR:
        # Best-effort enrichment only — habit_labels falls back to
        # habit_id below (see the .get(..., session["habit_id"]) default
        # a few lines down), so a failure here degrades display quality
        # rather than failing the whole review over a non-essential lookup.
        logger.warning("review_habit_week: list_habits enrichment failed", exc_info=True)
        habit_labels = {}

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
            "session_status": session.get("status", "pending"),
            "completed_at": session.get("completed_at"),
            "marked_by": session.get("marked_by"),
            "outcome": outcome,
        }
        if outcome != "kept":
            entry["bumped_by"] = _find_conflict(events, session)
        results.append(entry)

    # A1.4: telemetry only, never touches what's returned to the model
    # above — see _emit_habit_session_telemetry.
    _emit_habit_session_telemetry(tool_context, results)

    return {"status": "success", "sessions": results}


async def mark_habit_session(
    tool_context: ToolContext, calendar_id: str, event_id: str, status: str
) -> dict:
    """Mark a planned habit session completed or skipped, so "I did the
    gym this morning" or "I ended up skipping yoga" is captured as
    first-class state — never infer this yourself from the event still
    being on the calendar or from silence; only call this on the user's
    explicit say-so. Also use this to undo a mis-mark ("actually I didn't
    end up going" after marking it completed) by setting status back to
    "pending" — resetting to unknown is itself an explicit mark, same as
    any other transition, not something to avoid.

    calendar_id and event_id must come from a prior get_calendar_events,
    add_calendar_event, or review_habit_week result — never guess or ask
    the user for them; call review_habit_week or get_calendar_events first
    for the relevant period if you don't already have them from earlier in
    the conversation.

    Idempotent — marking a session with the status it already has is a
    no-op, safe to call again if you're not sure it registered.

    Args:
        calendar_id: The calendar the session's event lives on, from a
            prior result's "calendar_id" field.
        event_id: The session's event id, from the same prior result's
            "event_id" field.
        status: "completed", "skipped", or "pending" (to reset a mis-mark
            back to unknown).

    Returns:
        dict with "status" ("success", "not_found", or "error") and, on
        success, "session" (habit_id, event_id, calendar_id,
        session_status, completed_at, marked_by). "not_found" means no
        habit session is logged for that calendar_id/event_id — call
        review_habit_week or get_calendar_events first to find the right
        one rather than guessing. "error" means a backend failure
        prevented the mark from being recorded — not that it was
        rejected; the user's report should not be treated as saved.
    """
    try:
        session = await domain_client.set_habit_session_status(
            tool_context.session.user_id,
            calendar_id=calendar_id,
            event_id=event_id,
            status=status,
            marked_by="agent",
        )
    except domain_client.BACKEND_ERROR:
        logger.warning("mark_habit_session backend call failed", exc_info=True)
        return {
            "status": "error",
            "message": "Could not record this mark right now due to a backend error. Try again shortly.",
        }
    if session is None:
        return {
            "status": "not_found",
            "message": f"No habit session logged for event {event_id!r}.",
        }
    return {
        "status": "success",
        "session": {
            "habit_id": session["habit_id"],
            "event_id": session["event_id"],
            "calendar_id": session["calendar_id"],
            "session_status": session["status"],
            "completed_at": session.get("completed_at"),
            "marked_by": session.get("marked_by"),
        },
    }


def _emit_habit_session_telemetry(tool_context: ToolContext, sessions: list[dict]) -> None:
    """Best-effort: a failure here must never break review_habit_week's
    actual return value to the model. Writes to tool_context.state rather
    than returning anything — see _HABIT_SESSION_OUTCOMES_STATE_KEY above
    for why that's what reaches turn_log.py without costing the model a
    single token.

    Reads zones from the same session-state cache agent.py's
    _preload_zones already populated (zone_tools.PRELOADED_ZONES_STATE_KEY)
    rather than calling list_zones again — no extra backend round-trip on
    every review_habit_week call, at the cost of zone_constrained being
    based on a possibly-stale snapshot if the user changed a zone mid-
    session (the same staleness every other use of the preloaded zones
    already accepts). Missing entirely (session not preloaded yet, or A0.2
    preload failure) just means zone_constrained reads False for this
    turn's telemetry — a diagnostic slice being occasionally wrong is
    acceptable; it must never raise.

    "source": "organic" on every entry — this only ever runs from an
    agent- or user-triggered review_habit_week call; B2.2's future
    push-based path (Roadmap 2) will tag its own entries "push" into the
    same schema.

    "session_ref" (_hash_session_ref) gives each entry a stable identity
    so downstream queries can COUNT(DISTINCT session_ref) rather than
    COUNT(*): review_habit_week reports every session in its date range,
    and instruction.md has the agent calling it proactively before every
    new period, so the same session is re-emitted on every review that
    happens to cover it — without an identifier, a session reviewed three
    times silently counts three times.
    """
    try:
        zones = tool_context.state.get(zone_tools.PRELOADED_ZONES_STATE_KEY) or []

        outcomes = []
        for session in sessions:
            try:
                dt = datetime.fromisoformat(session["planned_start"])
            except (ValueError, TypeError, KeyError):
                continue
            outcomes.append(
                {
                    "session_ref": _hash_session_ref(session["calendar_id"], session["event_id"]),
                    "habit_id": session["habit_id"],
                    "session_status": session["session_status"],
                    "outcome": session["outcome"],
                    "hour_of_day": dt.hour,
                    "day_of_week": _WEEKDAY_CODES[dt.weekday()],
                    "zone_constrained": _zone_constrains(dt, zones),
                    "source": "organic",
                }
            )

        tool_context.state[_HABIT_SESSION_OUTCOMES_STATE_KEY] = outcomes
    except Exception:
        logger.warning("Failed to emit habit session telemetry", exc_info=True)


def _zone_constrains(dt: datetime, zones: list[dict]) -> bool:
    """Whether a session's planned start falls within a named zone's
    window — a diagnostic slice (A1.4's "whether a zone constrained the
    placement"), not a hard-constraint re-check. Doesn't handle a
    midnight-crossing zone (start_time > end_time as plain strings) —
    named zones like "Work"/"Commute" aren't typically that shape; sleep's
    own midnight-crossing window is a separate concept entirely (see
    zone_tools.get_sleep_schedule) and isn't part of this check."""
    day_code = _WEEKDAY_CODES[dt.weekday()]
    time_str = dt.strftime("%H:%M")
    for zone in zones:
        if day_code not in zone.get("days_of_week", []):
            continue
        if zone["start_time"] <= time_str < zone["end_time"]:
            return True
    return False


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
