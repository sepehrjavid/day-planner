"""Client for Vertex AI Agent Engine — the only thing in this codebase that
invokes the day planner agent.

The trust boundary this module exists to enforce: Agent Engine has no
concept of "who is allowed to claim to be user X" — whatever `user_id` this
process hands it is trusted completely, and whatever `session_id` this
process hands it resumes that session's full history. So `user_id` here
must always come from `current_user_id` (the caller's session token), and
`session_id` must always come from `Store.get_agent_session_id` (looked up
server-side, keyed by that same `user_id`) — never from a request body. See
../api/routes/chat.py, the only caller.
"""

from __future__ import annotations

import asyncio

import vertexai
from vertexai import agent_engines


class AgentClient:
    def __init__(self, *, project_id: str, location: str, reasoning_engine: str) -> None:
        # Just sets config globals — no network call, so unlike _get_app
        # below this is safe to do eagerly at construction time.
        vertexai.init(project=project_id, location=location)
        self._reasoning_engine = reasoning_engine
        self._app: agent_engines.AgentEngine | None = None
        self._lock = asyncio.Lock()

    async def _get_app(self) -> agent_engines.AgentEngine:
        """Built on first use, not at construction.

        agent_engines.get() makes a blocking describe call to fetch the
        resource's spec (which methods it exposes, etc.) — doing that at
        startup would make Agent Engine availability a hard dependency for
        /healthz, the same reasoning Store defers its Firestore client for.
        """
        if self._app is None:
            async with self._lock:
                if self._app is None:  # re-check: lost the race to another request
                    self._app = await asyncio.to_thread(
                        agent_engines.get, self._reasoning_engine
                    )
        return self._app

    async def send_message(
        self, *, user_id: str, session_id: str | None, message: str
    ) -> tuple[str, str]:
        """Send one message as `user_id`, in `session_id` if given.

        Returns (session_id, reply_text) — the session_id actually used,
        which the caller must persist if it differs from what it passed in
        (freshly created, or the given one had expired server-side).
        """
        app = await self._get_app()

        if session_id is not None:
            # A session_id that no longer exists (expired, or never existed)
            # would otherwise surface as an error out of async_stream_query
            # below — checking first turns that into "start a new session"
            # instead of a failed chat request.
            existing = await app.async_get_session(user_id=user_id, session_id=session_id)
            if existing is None:
                session_id = None

        if session_id is None:
            session = await app.async_create_session(user_id=user_id)
            session_id = session["id"]

        reply_parts: list[str] = []
        async for event in app.async_stream_query(
            user_id=user_id, session_id=session_id, message=message
        ):
            reply_parts.append(_visible_text(event))

        return session_id, "".join(reply_parts)


def _visible_text(event: dict) -> str:
    """The model-authored, user-visible text in one streamed ADK event.

    Tool call/response parts (function_call/function_response) carry no
    "text" field and drop out on their own; `thought` parts are Gemini's
    internal reasoning and are explicitly excluded. What's left is exactly
    what the agent actually said.
    """
    content = event.get("content") or {}
    if content.get("role") != "model":
        return ""
    return "".join(
        part.get("text", "")
        for part in content.get("parts", [])
        if part.get("text") and not part.get("thought")
    )
