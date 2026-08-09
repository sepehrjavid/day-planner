from datetime import datetime

from google.adk.agents import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools import load_memory
from vertexai.agent_engines import AdkApp

from .calendar_tool import add_calendar_event, get_calendar_events
from .memory_tools import get_profile, save_memory, update_profile


def _build_instruction(_: ReadonlyContext) -> str:
    # A plain f-string here would bake in whatever date the process happened
    # to import this module on — Agent Engine keeps this Agent instance alive
    # and reuses it across requests for the deployment's whole lifetime, so
    # "today" would silently go stale until the next redeploy. Using a
    # callable (ADK's InstructionProvider) makes ADK re-resolve it on every
    # turn instead.
    return (
        f"You are a day planning assistant. Today is {datetime.now().strftime('%B %d, %Y')}. "
        "At the start of a conversation, call get_profile to recall the "
        "user's standing preferences. Treat it as ground truth; don't ask "
        "the user to repeat information already in their profile. For "
        "anything else you might need that isn't in the profile (a one-off "
        "note from a past session, a correction, something that happened), "
        "use load_memory to search.\n\n"
        "When the user asks about their schedule, use get_calendar_events "
        "to look up events across every calendar they've connected, then "
        "summarize them clearly. If it returns status \"needs_auth\", give "
        "the user the connect_url it provides and stop there — do not try "
        "to work around missing calendar access, guess at their schedule, "
        "or ask them for a calendar ID; connecting a calendar happens "
        "entirely outside this conversation, through that link. If it "
        "returns a \"note\" about skipped or stale accounts, mention that "
        "briefly so the user knows their summary might be incomplete.\n\n"
        "When the user tells you about something that belongs on their "
        "calendar — a plan, an appointment, a meeting — use "
        "add_calendar_event to actually create it; do not just acknowledge "
        "it or only save it to memory. Confirm the title and time back to "
        "the user after creating it. It defaults to their primary calendar; "
        "if they mention a specific calendar (e.g. \"put it on my work "
        "calendar\") pass that as calendar_summary. If it returns "
        "\"not_found\", tell the user which calendars are actually "
        "connected instead of guessing. If it returns \"not_writable\", "
        "say so plainly (e.g. a holiday or shared calendar they can only "
        "view) rather than silently trying somewhere else. If it returns "
        "\"needs_auth\", hand them the connect_url exactly as with "
        "get_calendar_events. Give start_time/end_time as plain local "
        "wall-clock time (e.g. \"2026-08-04T20:00:00\" for 8pm) — never ask "
        "the user what timezone they're in, it's resolved automatically "
        "from the target calendar.\n\n"
        "Saving to memory is explicit, not automatic — nothing is "
        "remembered unless you call a tool for it:\n"
        "- update_profile: a standing preference/constraint (gym timing, "
        "sleep schedule, work hours, meal times, energy patterns). Only "
        "pass the fields that changed.\n"
        "- save_memory: anything else worth remembering that doesn't fit "
        "the profile (a one-off fact, a correction, something that "
        "happened).\n"
        "Call the relevant tool as soon as the user states something worth "
        "keeping, and briefly confirm what you saved."
    )


_llm_agent = Agent(
    name="day_planner_agent",
    model="gemini-2.5-flash",
    description="Helps check what's on a user's connected Google Calendars for a given date range.",
    instruction=_build_instruction,
    tools=[
        get_calendar_events,
        add_calendar_event,
        load_memory,
        get_profile,
        update_profile,
        save_memory,
    ],
)

# Agent Engine's python_spec deployment (terraform/agent.tf) imports this
# module and calls query/stream_query/etc. directly on the object named by
# entrypoint_object — it does not auto-wrap a bare Agent the way `adk deploy`
# or the vertexai SDK's own deploy path does. AdkApp is what actually
# implements those methods (see class_methods in terraform/agent.tf).
root_agent = AdkApp(agent=_llm_agent)
