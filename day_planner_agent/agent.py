from datetime import datetime

from google.adk.agents import Agent
from google.adk.tools import load_memory
from vertexai.agent_engines import AdkApp

from .calendar_tool import get_calendar_events
from .memory_tools import get_profile, save_memory, update_profile

_llm_agent = Agent(
    name="day_planner_agent",
    model="gemini-2.5-flash",
    description="Helps check what's on a user's connected Google Calendars for a given date range.",
    instruction=(
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
    ),
    tools=[get_calendar_events, load_memory, get_profile, update_profile, save_memory],
)

# Agent Engine's python_spec deployment (terraform/agent.tf) imports this
# module and calls query/stream_query/etc. directly on the object named by
# entrypoint_object — it does not auto-wrap a bare Agent the way `adk deploy`
# or the vertexai SDK's own deploy path does. AdkApp is what actually
# implements those methods (see class_methods in terraform/agent.tf).
root_agent = AdkApp(agent=_llm_agent)
