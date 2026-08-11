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

# Session-state keys the preload callback below writes to and the
# instruction reads from. Prefixed so they don't collide with anything a
# tool call might stash in state.
_PROFILE_PRELOADED_KEY = "day_planner:profile_preloaded"
_PRELOADED_PROFILE_KEY = "day_planner:preloaded_profile"

# The system prompt is long-form prose, not code — kept in its own file so
# it reads and diffs like the rest of the agent's copy, instead of being
# buried in string-concatenation. Loaded once at import time since
# build_archive.sh ships it alongside agent.py either way.
_INSTRUCTION_TEMPLATE = (Path(__file__).parent / "instruction.md").read_text()


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
    """
    if callback_context.state.get(_PROFILE_PRELOADED_KEY):
        return
    callback_context.state[_PROFILE_PRELOADED_KEY] = True

    result = await get_profile(callback_context)
    if result.get("status") == "success" and result.get("profile"):
        callback_context.state[_PRELOADED_PROFILE_KEY] = result["profile"]


def _build_instruction(ctx: ReadonlyContext) -> str:
    # A plain f-string here would bake in whatever date the process happened
    # to import this module on — Agent Engine keeps this Agent instance alive
    # and reuses it across requests for the deployment's whole lifetime, so
    # "today" would silently go stale until the next redeploy. Using a
    # callable (ADK's InstructionProvider) makes ADK re-resolve it on every
    # turn instead.
    preloaded_profile = ctx.state.get(_PRELOADED_PROFILE_KEY)
    profile_section = (
        f"The user's standing preferences, already loaded for this "
        f"session: {preloaded_profile}\n\n"
        if preloaded_profile
        else "No standing preferences are on file for this user yet.\n\n"
    )
    return _INSTRUCTION_TEMPLATE.format(
        today=datetime.now().strftime("%B %d, %Y"),
        profile_section=profile_section,
    )


_llm_agent = Agent(
    name="day_planner_agent",
    model="gemini-2.5-flash",
    description=(
        "Manages a user's connected Google Calendars — checking, creating, "
        "updating, and deleting events — proactively schedules their "
        "tracked habits (e.g. weekly exercise targets) onto the calendar, "
        "and reviews how well past habit sessions actually held up."
    ),
    instruction=_build_instruction,
    before_agent_callback=_preload_profile,
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
    ],
)

# Agent Engine's python_spec deployment (terraform/agent.tf) imports this
# module and calls query/stream_query/etc. directly on the object named by
# entrypoint_object — it does not auto-wrap a bare Agent the way `adk deploy`
# or the vertexai SDK's own deploy path does. AdkApp is what actually
# implements those methods (see class_methods in terraform/agent.tf).
root_agent = AdkApp(agent=_llm_agent)
