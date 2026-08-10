"""The routes that talk to the day planner agent.

user_id comes from `current_user_id` (the session token) and nothing else —
the same rule every other /me route follows, extended one hop further here
because the stakes are higher: Agent Engine has no way to check whether the
caller is actually allowed to be the `user_id` it's handed, so this route is
the entire trust boundary. A user_id in the request body, instead of the
dependency below, would let any signed-in caller read or continue anyone
else's conversation (and, through it, their calendar). See
../../services/agent_client.py for the same rule applied to session_id.

Session lifecycle: one active Agent Engine session per user, rolled over
after settings.agent_session_idle_timeout_seconds of inactivity (default 6h
— see core/config.py for why), or on demand via /me/chat/reset. Either way
the outgoing session is archived to Memory Bank before being dropped, so a
rollover loses conversational continuity but not anything the agent decided
was worth remembering. See AgentClient.archive_session.

Quota: every message first spends one of settings.chat_daily_quota for the
day, checked and consumed atomically in Store.check_and_consume_quota. A
caller past their limit gets a 429 before the agent is ever touched.
"""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status

from ...core.config import Settings, get_settings
from ...db.models import utcnow
from ...db.store import Store
from ...schemas.chat import ChatRequest, ChatResponse
from ...services.agent_client import AgentClient
from ..deps import current_user_id, get_agent_client, get_store

router = APIRouter(prefix="/me", tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
    agent_client: AgentClient = Depends(get_agent_client),
    settings: Settings = Depends(get_settings),
) -> ChatResponse:
    # Checked (and consumed) before touching the agent, so a caller over
    # quota never costs a model call.
    quota = await store.check_and_consume_quota(
        user_id, daily_limit=settings.chat_daily_quota
    )
    if not quota.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="daily message quota exceeded",
            headers={"Retry-After": str(quota.retry_after_seconds)},
        )

    session_id, last_active_at = await store.get_agent_session(user_id)

    if session_id is not None and _is_idle(
        last_active_at, settings.agent_session_idle_timeout_seconds
    ):
        await agent_client.archive_session(user_id=user_id, session_id=session_id)
        session_id = None

    resolved_session_id, reply = await agent_client.send_message(
        user_id=user_id, session_id=session_id, message=body.message
    )

    await store.set_agent_session(user_id=user_id, session_id=resolved_session_id)

    return ChatResponse(reply=reply, new_session=resolved_session_id != session_id)


@router.post("/chat/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_chat(
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
    agent_client: AgentClient = Depends(get_agent_client),
) -> None:
    """Start fresh on demand — the manual counterpart to the idle rollover
    above, for when a conversation has gone stale before the timeout would
    have caught it (or the user just wants a clean slate right now)."""
    session_id, _ = await store.get_agent_session(user_id)
    if session_id is not None:
        await agent_client.archive_session(user_id=user_id, session_id=session_id)
    await store.clear_agent_session(user_id)


def _is_idle(last_active_at, timeout_seconds: int) -> bool:
    if last_active_at is None:
        return False
    return utcnow() - last_active_at > timedelta(seconds=timeout_seconds)
