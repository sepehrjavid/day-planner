"""The one route that talks to the day planner agent.

user_id comes from `current_user_id` (the session token) and nothing else —
the same rule every other /me route follows, extended one hop further here
because the stakes are higher: Agent Engine has no way to check whether the
caller is actually allowed to be the `user_id` it's handed, so this route is
the entire trust boundary. A user_id in the request body, instead of the
dependency below, would let any signed-in caller read or continue anyone
else's conversation (and, through it, their calendar). See
../../services/agent_client.py for the same rule applied to session_id.
"""

from fastapi import APIRouter, Depends

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
) -> ChatResponse:
    session_id = await store.get_agent_session_id(user_id)

    resolved_session_id, reply = await agent_client.send_message(
        user_id=user_id, session_id=session_id, message=body.message
    )

    if resolved_session_id != session_id:
        await store.set_agent_session_id(user_id=user_id, session_id=resolved_session_id)

    return ChatResponse(reply=reply)
