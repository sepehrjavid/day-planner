"""Request/response models for /me/chat and /me/chat/reset.

Deliberately no session_id anywhere here, in either direction. Accepting one
in the request would let a client resume (and thus read the history of) any
Agent Engine session it can guess the id of; returning one would just invite
a future caller to start passing it back. The session is resolved entirely
server-side — see app/db/store.py's get_agent_session/set_agent_session and
app/api/routes/chat.py's idle-timeout rollover.
"""

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    reply: str
    # Not the session_id itself — just whether this message landed in a
    # fresh session (idle timeout, or the very first message ever). A
    # client can use this to show "starting a new conversation" without
    # ever being trusted with the id that would let it resume one.
    new_session: bool
