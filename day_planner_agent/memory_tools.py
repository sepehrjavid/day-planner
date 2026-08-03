"""Explicit, agent-controlled Memory Bank tools.

The agent decides when something is worth persisting and calls these
directly, instead of every turn being sent to Memory Bank's extraction
pipeline regardless of content.

Two kinds of long-term memory:
  - The structured profile (preferences only) — a single, low-latency
    source of truth, written via update_profile and read via get_profile.
    See scripts/register_profile_schema.py for the one-time schema
    registration this depends on. Calendar identity is deliberately not
    part of this profile — it lives in day_planner_backend_internal
    instead, since Memory Bank is LLM-written and semantically searched,
    which is the wrong access model for anything credential-adjacent (see
    ../oauth-design.md §7).
  - Free-form facts that don't fit the profile — saved via save_memory,
    searched via ADK's built-in load_memory tool.

Write calls (generate/create) go straight to the Vertex AI SDK rather than
through ADK's VertexAiMemoryBankService wrapper: that wrapper builds its
request config through a key-introspection helper that, on the installed
SDK versions, silently drops wait_for_completion — the write appears to
succeed but nothing gets generated/stored, with no error raised. Read calls
(retrieve_profiles, search_memory via load_memory) are plain passthroughs
in that wrapper with no such bug, so those are used as-is.
"""

import logging
from typing import Optional

import vertexai
from google.adk.tools import ToolContext
from google.genai import types as genai_types

logger = logging.getLogger(__name__)

PROFILE_SCHEMA_ID = "day-planner-profile"


def _memory_service(tool_context: ToolContext):
    service = tool_context._invocation_context.memory_service
    if service is None or not hasattr(service, "_agent_engine_id"):
        return None
    return service


def _memory_scope(tool_context: ToolContext) -> dict:
    return {
        "app_name": tool_context.session.app_name,
        "user_id": tool_context.session.user_id,
    }


async def update_profile(
    tool_context: ToolContext,
    preferences: Optional[str] = None,
) -> dict:
    """Save or update the user's structured profile in long-term memory.

    Call this whenever the user states or corrects a standing
    preference/constraint worth remembering across sessions (gym
    duration/timing, sleep schedule, work hours, meal times, energy
    patterns, etc). Only pass the fields that changed — this merges into
    the existing profile rather than replacing it.

    Calendar connections are not part of this profile and never go through
    this tool — see get_calendar_events, which returns a connect_url when a
    user needs to link a calendar. That flow lives entirely outside this
    conversation, in day_planner_backend_internal.

    Args:
        preferences: Free-text preference/constraint update, in your own
            words (e.g. "gym sessions are 1 hour for upper body days").

    Returns:
        dict with "status" ("success" or "error") and "message".
    """
    service = _memory_service(tool_context)
    if service is None:
        return {"status": "error", "message": "Memory Bank is not configured."}

    statements = []
    if preferences:
        statements.append(f"User preference: {preferences}")
    if not statements:
        return {"status": "error", "message": "No fields provided to save."}

    print("Statements: ", statements)

    client = vertexai.Client(project=service._project, location=service._location).aio
    operation = await client.agent_engines.memories.generate(
        name="reasoningEngines/" + service._agent_engine_id,
        direct_contents_source=vertexai.types.GenerateMemoriesRequestDirectContentsSource(
            events=[
                vertexai.types.GenerateMemoriesRequestDirectContentsSourceEvent(
                    content=genai_types.Content(
                        role="user",
                        parts=[genai_types.Part(text=" ".join(statements))],
                    )
                )
            ]
        ),
        scope=_memory_scope(tool_context),
        config={"wait_for_completion": True},
    )
    if operation.error:
        logger.warning("update_profile failed: %s", operation.error)
        return {"status": "error", "message": str(operation.error)}

    logger.info("update_profile: generate response %s", operation.response)
    return {"status": "success", "message": "Profile updated."}


async def get_profile(tool_context: ToolContext) -> dict:
    """Retrieve the user's structured profile from long-term memory.

    Call this at the start of a conversation so you don't have to ask the
    user to repeat things you already know about them: their standing
    preferences.

    Returns:
        dict with "status" and "profile" (the field dict, or {} if no
        profile exists yet for this user).
    """
    service = _memory_service(tool_context)
    if service is None:
        return {
            "status": "error",
            "message": "Memory Bank is not configured.",
            "profile": {},
        }

    profiles = await service.retrieve_profiles(**_memory_scope(tool_context))
    for profile in profiles:
        if profile.schema_id == PROFILE_SCHEMA_ID:
            print("Profile: ", profile.profile)
            return {"status": "success", "profile": profile.profile or {}}
    return {"status": "success", "profile": {}}


async def save_memory(tool_context: ToolContext, fact: str) -> dict:
    """Save a standalone fact to long-term memory (not part of the
    structured profile) — e.g. a one-off note, correction, or something
    that happened worth remembering later.

    Args:
        fact: The fact to remember, as a short, self-contained statement.

    Returns:
        dict with "status" ("success" or "error") and "message".
    """
    service = _memory_service(tool_context)
    if service is None:
        return {"status": "error", "message": "Memory Bank is not configured."}

    client = vertexai.Client(project=service._project, location=service._location).aio
    operation = await client.agent_engines.memories.create(
        name="reasoningEngines/" + service._agent_engine_id,
        fact=fact,
        scope=_memory_scope(tool_context),
        config={"wait_for_completion": True},
    )
    if operation.error:
        logger.warning("save_memory failed: %s", operation.error)
        return {"status": "error", "message": str(operation.error)}

    return {"status": "success", "message": "Saved."}
