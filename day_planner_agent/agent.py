import logging
from datetime import datetime
from pathlib import Path

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools import load_memory
from vertexai.agent_engines import AdkApp

from .calendar_tool import (
    add_calendar_event,
    delete_calendar_event,
    get_calendar_events,
    update_calendar_event,
)
from .habit_tools import create_habit, list_habits, review_habit_week, update_habit
from .memory_tools import get_profile, save_memory, update_profile
from .zone_tools import (
    create_zone,
    get_sleep_schedule,
    list_zones,
    set_sleep_schedule,
    update_zone,
)

logger = logging.getLogger(__name__)

# Session-state keys the preload callbacks below write to and the
# instruction reads from. Prefixed so they don't collide with anything a
# tool call might stash in state.
_PROFILE_PRELOADED_KEY = "day_planner:profile_preloaded"
_PRELOADED_PROFILE_KEY = "day_planner:preloaded_profile"
_PROFILE_PRELOAD_FAILED_KEY = "day_planner:profile_preload_failed"
_ZONES_PRELOADED_KEY = "day_planner:zones_preloaded"
_PRELOADED_ZONES_KEY = "day_planner:preloaded_zones"
_PRELOADED_SLEEP_SCHEDULE_KEY = "day_planner:preloaded_sleep_schedule"
_ZONES_PRELOAD_FAILED_KEY = "day_planner:zones_preload_failed"

# Read by A1.1's turn-record logging — True only when neither preload has a
# failure latched for this session; see _refresh_preload_ok.
_PRELOAD_OK_KEY = "day_planner:preload_ok"

# The system prompt is long-form prose, not code — kept in its own file so
# it reads and diffs like the rest of the agent's copy, instead of being
# buried in string-concatenation. Loaded once at import time since
# build_archive.sh ships it alongside agent.py either way.
_INSTRUCTION_TEMPLATE = (Path(__file__).parent / "instruction.md").read_text()


def _now() -> datetime:
    """A module attribute rather than a bare datetime.now() call so a test
    can pin "today" by monkeypatching agent._now — see _build_instruction,
    which must re-resolve this every turn rather than capture it once."""
    return datetime.now()


def _refresh_preload_ok(callback_context: CallbackContext) -> None:
    state = callback_context.state
    failed = state.get(_PROFILE_PRELOAD_FAILED_KEY, False) or state.get(
        _ZONES_PRELOAD_FAILED_KEY, False
    )
    state[_PRELOAD_OK_KEY] = not failed


async def _preload_profile(callback_context: CallbackContext) -> None:
    """Fetches the user's profile once per session, before the model ever
    sees the first turn.

    Telling the model in its instructions to "call get_profile at the
    start of a conversation" is a suggestion, not a guarantee — an LLM can
    and does skip it, which meant the agent sometimes claimed to not know
    preferences that were actually on file. Running this as a
    before_agent_callback makes the first turn's profile lookup
    unconditional instead of dependent on the model choosing to act on an
    instruction. Later turns are a cheap state-flag check away from a
    no-op.

    The preloaded flag is only set on a *successful* fetch, and any
    exception is caught rather than left to propagate and kill the
    invocation — a transient failure here must lead to a retry on the next
    turn, not a flag latched True forever with nothing behind it (that
    would look identical to "this user genuinely has no profile yet").
    """
    if callback_context.state.get(_PROFILE_PRELOADED_KEY):
        return

    try:
        result = await get_profile(callback_context)
    except Exception:
        logger.warning("Profile preload failed", exc_info=True)
        callback_context.state[_PROFILE_PRELOAD_FAILED_KEY] = True
        _refresh_preload_ok(callback_context)
        return

    if result.get("status") != "success":
        logger.warning(
            "Profile preload returned non-success status: %s", result.get("status")
        )
        callback_context.state[_PROFILE_PRELOAD_FAILED_KEY] = True
        _refresh_preload_ok(callback_context)
        return

    callback_context.state[_PROFILE_PRELOADED_KEY] = True
    callback_context.state[_PROFILE_PRELOAD_FAILED_KEY] = False
    if result.get("profile"):
        callback_context.state[_PRELOADED_PROFILE_KEY] = result["profile"]
    _refresh_preload_ok(callback_context)


async def _preload_zones(callback_context: CallbackContext) -> None:
    """Fetches the user's zones and sleep schedule once per session, the
    same way _preload_profile does for the profile above and for the same
    reason: zones are low-cardinality and change rarely (docs/todo.md
    §1), so an unconditional fetch here is cheap, and it means placement
    doesn't depend on the model remembering to call list_zones/
    get_sleep_schedule itself before ever placing a session.

    Zones and the sleep schedule are hard constraints (see instruction.md),
    so failing open here is worse than failing open on the profile: it was
    possible for a transient backend error to latch the preloaded flag True
    with nothing fetched, which made _build_instruction claim "no day zones
    or sleep schedule are on file" and the agent would then schedule
    straight through work hours and sleep. The flag is now only set after
    both fetches succeed, and any exception is caught and recorded as a
    distinct failure state instead.
    """
    if callback_context.state.get(_ZONES_PRELOADED_KEY):
        return

    try:
        zones_result = await list_zones(callback_context)
        sleep_result = await get_sleep_schedule(callback_context)
    except Exception:
        logger.warning("Zones/sleep preload failed", exc_info=True)
        callback_context.state[_ZONES_PRELOAD_FAILED_KEY] = True
        _refresh_preload_ok(callback_context)
        return

    if zones_result.get("status") != "success" or sleep_result.get("status") != "success":
        logger.warning(
            "Zones/sleep preload returned non-success status: zones=%s sleep=%s",
            zones_result.get("status"),
            sleep_result.get("status"),
        )
        callback_context.state[_ZONES_PRELOAD_FAILED_KEY] = True
        _refresh_preload_ok(callback_context)
        return

    callback_context.state[_ZONES_PRELOADED_KEY] = True
    callback_context.state[_ZONES_PRELOAD_FAILED_KEY] = False
    if zones_result.get("zones"):
        callback_context.state[_PRELOADED_ZONES_KEY] = zones_result["zones"]
    if sleep_result.get("exists"):
        callback_context.state[_PRELOADED_SLEEP_SCHEDULE_KEY] = sleep_result["schedule"]
    _refresh_preload_ok(callback_context)


def _build_instruction(ctx: ReadonlyContext) -> str:
    # A plain f-string here would bake in whatever date the process happened
    # to import this module on — Agent Engine keeps this Agent instance alive
    # and reuses it across requests for the deployment's whole lifetime, so
    # "today" would silently go stale until the next redeploy. Using a
    # callable (ADK's InstructionProvider) makes ADK re-resolve it on every
    # turn instead.
    preloaded_profile = ctx.state.get(_PRELOADED_PROFILE_KEY)
    if ctx.state.get(_PROFILE_PRELOAD_FAILED_KEY):
        profile_section = (
            "Standing preferences could not be loaded for this session due "
            "to a backend error — this is not the same as the user having "
            "none on file. Do not tell the user their preferences are "
            "unknown or missing; if you need one, call get_profile "
            "yourself to re-check before relying on its absence.\n\n"
        )
    elif preloaded_profile:
        profile_section = (
            f"The user's standing preferences, already loaded for this "
            f"session: {preloaded_profile}\n\n"
        )
    else:
        profile_section = "No standing preferences are on file for this user yet.\n\n"

    preloaded_zones = ctx.state.get(_PRELOADED_ZONES_KEY)
    preloaded_sleep_schedule = ctx.state.get(_PRELOADED_SLEEP_SCHEDULE_KEY)
    if ctx.state.get(_ZONES_PRELOAD_FAILED_KEY):
        zones_section = (
            "Day zones and the sleep schedule could not be loaded for this "
            "session due to a backend error. These are hard constraints — "
            "do not assume the user has none just because nothing loaded. "
            "Avoid placing or moving any habit session until you have "
            "successfully re-checked with list_zones and "
            "get_sleep_schedule.\n\n"
        )
    elif preloaded_zones or preloaded_sleep_schedule:
        zones_section = (
            f"The user's standing day zones, already loaded for this session: "
            f"{preloaded_zones or []}\n"
            f"Their sleep schedule, already loaded for this session: "
            f"{preloaded_sleep_schedule or 'not set'}\n\n"
        )
    else:
        zones_section = (
            "No day zones or sleep schedule are on file for this user yet.\n\n"
        )

    return _INSTRUCTION_TEMPLATE.format(
        today=_now().strftime("%B %d, %Y"),
        profile_section=profile_section,
        zones_section=zones_section,
    )


_llm_agent = Agent(
    name="day_planner_agent",
    model="gemini-2.5-flash",
    description=(
        "Manages a user's connected Google Calendars — checking, creating, "
        "updating, and deleting events — proactively schedules their "
        "tracked habits (e.g. weekly exercise targets) onto the calendar "
        "around their day zones (work hours, sleep, and any other "
        "standing restriction), and reviews how well past habit sessions "
        "actually held up."
    ),
    instruction=_build_instruction,
    before_agent_callback=[_preload_profile, _preload_zones],
    tools=[
        get_calendar_events,
        add_calendar_event,
        update_calendar_event,
        delete_calendar_event,
        load_memory,
        get_profile,
        update_profile,
        save_memory,
        create_habit,
        list_habits,
        update_habit,
        review_habit_week,
        create_zone,
        list_zones,
        update_zone,
        set_sleep_schedule,
        get_sleep_schedule,
    ],
)

# Agent Engine's python_spec deployment (terraform/agent.tf) imports this
# module and calls query/stream_query/etc. directly on the object named by
# entrypoint_object — it does not auto-wrap a bare Agent the way `adk deploy`
# or the vertexai SDK's own deploy path does. AdkApp is what actually
# implements those methods (see class_methods in terraform/agent.tf).
root_agent = AdkApp(agent=_llm_agent)
