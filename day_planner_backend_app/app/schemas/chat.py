"""Request/response models for /me/chat.

Deliberately no session_id anywhere here, in either direction. Accepting one
in the request would let a client resume (and thus read the history of) any
Agent Engine session it can guess the id of; returning one would just invite
a future caller to start passing it back. The session is resolved entirely
server-side — see app/db/store.py's get_agent_session_id/set_agent_session_id.
"""

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    reply: str
