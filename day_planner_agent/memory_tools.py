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
    ../docs/oauth-design.md §7).
  - Free-form facts that don't fit the profile — saved via save_memory,
    searched via ADK's built-in load_memory tool.

A third kind of long-term state — the user's tracked habits (recurring,
placeable goals like "180 min/week of exercise") — deliberately isn't here:
see habit_tools.py, which stores them in day_planner_backend_internal for
the same reason calendar identity lives there instead of Memory Bank.

Write calls (generate/create) go straight to the Vertex AI SDK rather than
through ADK's VertexAiMemoryBankService wrapper: that wrapper builds its
request config through a key-introspection helper that, on the installed
SDK versions, silently drops wait_for_completion — the write appears to
succeed but nothing gets generated/stored, with no error raised. Read calls
(retrieve_profiles, search_memory via load_memory) are plain passthroughs
in that wrapper with no such bug, so those are used as-is.

wait_for_completion=True is correct and necessary (it's the only thing
standing between this module and the exact bug described above) — but it
also means the call blocks until Memory Bank's server-side extraction
pipeline finishes, which is multi-second and was stalling the whole turn
on it (A2.4). The fix isn't to drop wait_for_completion; it's to stop
awaiting the write inline. update_profile/save_memory schedule it as a
background task (_schedule_background_write) and return as soon as it's
been dispatched, not once it's done — the actual generate()/create() call
underneath is byte-for-byte the same blocking, wait_for_completion=True
call as before, just no longer on the path the model is waiting on. This
relies on the same standing assumption as A2.1's module-level HTTP
client/token cache: the process this runs in (Agent Engine) is long-lived
across turns, not spun up fresh per request, so a task scheduled here
keeps running after this turn's response has gone out.
"""

import asyncio
import logging
import random
from typing import Optional

import google.api_core.exceptions
import google.auth.exceptions
import vertexai
from google.adk.tools import ToolContext
from google.genai import types as genai_types

logger = logging.getLogger(__name__)

# A2.6: Memory Bank's own SDK, not backend_client — the equivalent tuple
# to backend_client.BACKEND_ERROR for this module's dependency. Google's
# Cloud client libraries raise GoogleAPIError (and subclasses) for a
# failed API call; GoogleAuthError covers the underlying credential/
# token-mint failure the same way it does for backend_client.
_MEMORY_BANK_ERROR = (google.api_core.exceptions.GoogleAPIError, google.auth.exceptions.GoogleAuthError)

PROFILE_SCHEMA_ID = "day-planner-profile"

# A background write's own failure has nowhere left to report to — the
# tool call it belongs to already returned — so it gets a few attempts of
# its own before giving up. Not shared with calendar_tool.py's retry logic
# (A2.3): that one keys off HTTP status codes and needs an idempotency
# key to be safe to retry at all; this one is a same-payload SDK call
# where a merged, LLM-driven extraction pipeline is expected to tolerate
# a harmless duplicate submission, so a flat retry is enough.
_BACKGROUND_WRITE_MAX_ATTEMPTS = 3
_BACKGROUND_WRITE_BASE_DELAY_S = 1.0

# Module attribute rather than a bare asyncio.sleep call so tests can swap
# it for a no-op — same pattern as agent.py's _now() (A0.4) and
# calendar_tool.py's _sleep (A2.3).
_sleep = asyncio.sleep

# A fire-and-forget asyncio.Task with no other strong reference can be
# garbage-collected mid-flight ("Task was destroyed but it is pending") —
# this set plus the done-callback in _schedule_background_write is
# asyncio's own documented idiom for avoiding that.
_pending_writes: set[asyncio.Task] = set()


def _schedule_background_write(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _pending_writes.add(task)
    task.add_done_callback(_pending_writes.discard)
    return task


async def _write_with_retry(kind: str, attempt_write) -> None:
    """Runs after the tool that scheduled it has already returned to the
    model — a failure here can only be logged, never surfaced to the
    caller. attempt_write is a zero-arg async callable, called fresh each
    attempt (a coroutine object can only be awaited once)."""
    for attempt in range(1, _BACKGROUND_WRITE_MAX_ATTEMPTS + 1):
        try:
            operation = await attempt_write()
        except Exception:
            logger.warning(
                "%s failed (attempt %d/%d)",
                kind,
                attempt,
                _BACKGROUND_WRITE_MAX_ATTEMPTS,
                exc_info=True,
            )
        else:
            if operation.error is None:
                return
            logger.warning(
                "%s failed (attempt %d/%d): %s",
                kind,
                attempt,
                _BACKGROUND_WRITE_MAX_ATTEMPTS,
                operation.error,
            )
        if attempt < _BACKGROUND_WRITE_MAX_ATTEMPTS:
            await _sleep(_BACKGROUND_WRITE_BASE_DELAY_S * attempt + random.uniform(0, 0.5))
    logger.error("%s permanently failed after %d attempts", kind, _BACKGROUND_WRITE_MAX_ATTEMPTS)


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
    preference/constraint that has no target of its own — it only shapes
    *when* or *how* things (including habits) get scheduled. Examples: "I
    prefer not to have physical activities after 8pm" (a blackout window),
    "I don't want two exercise sessions in the same day" (a cross-habit
    scheduling rule), plus gym timing, sleep schedule, work hours, meal
    times, energy patterns.

    Do not use this for a recurring goal with its own frequency/duration
    target that should become blocked time on the calendar (e.g. "90
    minutes of gym a week") — that's a habit; call create_habit/
    update_habit instead (habit_tools.py), even if it's phrased like a
    preference. Also not for a single day's plan, which belongs on the
    calendar instead. Only pass the fields that changed — this merges into
    the existing profile rather than replacing it.

    The profile must stay minimal: one current statement per topic, never
    two overlapping ones. If the user is changing a preference already in
    their profile (check get_profile first), write preferences as the full
    corrected statement for that topic so it supersedes the stale one,
    rather than appending a second, conflicting statement about the same
    thing.

    Calendar connections are not part of this profile and never go through
    this tool — see get_calendar_events, which returns a connect_url when a
    user needs to link a calendar. That flow lives entirely outside this
    conversation, in day_planner_backend_internal.

    Args:
        preferences: Free-text preference/constraint update, in your own
            words (e.g. "gym sessions are 1 hour for upper body days").

    Returns:
        dict with "status" ("success" or "error") and "message". "success"
        means the update was accepted and is saving in the background, not
        that it has finished — get_profile called immediately afterward is
        not guaranteed to see it yet.
    """
    service = _memory_service(tool_context)
    if service is None:
        return {"status": "error", "message": "Memory Bank is not configured."}

    statements = []
    if preferences:
        statements.append(f"User preference: {preferences}")
    if not statements:
        return {"status": "error", "message": "No fields provided to save."}

    try:
        client = vertexai.Client(project=service._project, location=service._location).aio
    except _MEMORY_BANK_ERROR:
        logger.warning("update_profile client construction failed", exc_info=True)
        return {
            "status": "error",
            "message": "Could not save this right now due to a backend error. Try again shortly.",
        }
    scope = _memory_scope(tool_context)
    agent_engine_id = service._agent_engine_id

    async def _write():
        return await client.agent_engines.memories.generate(
            name="reasoningEngines/" + agent_engine_id,
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
            scope=scope,
            config={"wait_for_completion": True},
        )

    _schedule_background_write(_write_with_retry("update_profile", _write))

    return {"status": "success", "message": "Saving profile."}


async def get_profile(tool_context: ToolContext) -> dict:
    """Retrieve the user's structured profile from long-term memory.

    Call this at the start of a conversation so you don't have to ask the
    user to repeat things you already know about them: their standing
    preferences.

    Returns:
        dict with "status" and "profile" (the field dict, or {} if no
        profile exists yet for this user). On "error" due to a backend
        failure, "profile" is omitted entirely rather than set to {} —
        that would be indistinguishable from the user genuinely having
        none on file; do not tell the user their preferences are unknown
        or missing on the strength of this call.
    """
    service = _memory_service(tool_context)
    if service is None:
        return {
            "status": "error",
            "message": "Memory Bank is not configured.",
            "profile": {},
        }

    try:
        profiles = await service.retrieve_profiles(**_memory_scope(tool_context))
    except _MEMORY_BANK_ERROR:
        logger.warning("get_profile backend call failed", exc_info=True)
        return {
            "status": "error",
            "message": (
                "Could not load the profile right now due to a backend error — "
                "this does not mean the user has no preferences on file. Retry "
                "before assuming any preference is unknown or missing."
            ),
        }
    for profile in profiles:
        if profile.schema_id == PROFILE_SCHEMA_ID:
            return {"status": "success", "profile": profile.profile or {}}
    return {"status": "success", "profile": {}}


async def save_memory(tool_context: ToolContext, fact: str) -> dict:
    """Save a standalone fact to long-term memory (not part of the
    structured profile) — e.g. a one-off note, correction, or something
    that happened worth remembering later.

    Args:
        fact: The fact to remember, as a short, self-contained statement.

    Returns:
        dict with "status" ("success" or "error") and "message". "success"
        means the fact was accepted and is saving in the background, not
        that it has finished.
    """
    service = _memory_service(tool_context)
    if service is None:
        return {"status": "error", "message": "Memory Bank is not configured."}

    try:
        client = vertexai.Client(project=service._project, location=service._location).aio
    except _MEMORY_BANK_ERROR:
        logger.warning("save_memory client construction failed", exc_info=True)
        return {
            "status": "error",
            "message": "Could not save this right now due to a backend error. Try again shortly.",
        }
    scope = _memory_scope(tool_context)
    agent_engine_id = service._agent_engine_id

    async def _write():
        return await client.agent_engines.memories.create(
            name="reasoningEngines/" + agent_engine_id,
            fact=fact,
            scope=scope,
            config={"wait_for_completion": True},
        )

    _schedule_background_write(_write_with_retry("save_memory", _write))

    return {"status": "success", "message": "Saving."}
