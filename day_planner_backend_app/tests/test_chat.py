"""Coverage of /me/chat's trust boundary.

The behaviour worth pinning down isn't the happy path (send a message, get a
reply back) — it's that user_id and session_id are never anything other than
what current_user_id and Store.get_agent_session_id resolve them to,
regardless of what a client puts in the request body. See
app/services/agent_client.py and app/api/routes/chat.py for why that's the
whole point of this route.
"""

import pytest

from app import main


class FakeAgentClient:
    """Records every call so tests can assert on user_id/session_id without
    a real Agent Engine deployment."""

    def __init__(self):
        self.calls: list[dict] = []
        self._next_session = 0

    async def send_message(self, *, user_id, session_id, message):
        self.calls.append(
            {"user_id": user_id, "session_id": session_id, "message": message}
        )
        if session_id is None:
            self._next_session += 1
            session_id = f"session-{self._next_session}"
        return session_id, f"echo: {message}"


@pytest.fixture
def agent_client(anon_client):
    fake = FakeAgentClient()
    main.app.state.agent_client = fake
    return fake


def test_requires_auth(anon_client, agent_client):
    response = anon_client.post("/me/chat", json={"message": "hi"})
    assert response.status_code == 401
    assert agent_client.calls == []


def test_identity_comes_from_session_token_not_body(anon_client, user, agent_client):
    user_id, headers = user

    # A client-supplied user_id in the body must be silently ignored — the
    # schema doesn't even define the field, so this also pins down that
    # smuggling one in doesn't somehow reach the agent.
    response = anon_client.post(
        "/me/chat",
        json={"message": "what's on my calendar?", "user_id": "someone-else"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert len(agent_client.calls) == 1
    assert agent_client.calls[0]["user_id"] == user_id


def test_first_message_creates_and_persists_a_session(anon_client, user, agent_client, store):
    user_id, headers = user
    assert "agent_session_id" not in store.users[user_id]

    response = anon_client.post("/me/chat", json={"message": "hi"}, headers=headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"reply": "echo: hi"}
    assert agent_client.calls[0]["session_id"] is None
    assert store.users[user_id]["agent_session_id"] == "session-1"


def test_second_message_reuses_the_persisted_session(anon_client, user, agent_client):
    user_id, headers = user

    first = anon_client.post("/me/chat", json={"message": "hi"}, headers=headers)
    second = anon_client.post("/me/chat", json={"message": "again"}, headers=headers)

    assert first.status_code == second.status_code == 200
    assert agent_client.calls[0]["session_id"] is None
    assert agent_client.calls[1]["session_id"] == "session-1"


def test_two_users_never_share_a_session(anon_client, agent_client):
    def signup(email):
        response = anon_client.post(
            "/auth/signup", json={"email": email, "password": "correct-horse-battery"}
        )
        body = response.json()
        return body["user_id"], {"Authorization": f"Bearer {body['access_token']}"}

    alice_id, alice_headers = signup("alice@example.com")
    bob_id, bob_headers = signup("bob@example.com")

    anon_client.post("/me/chat", json={"message": "hi"}, headers=alice_headers)
    anon_client.post("/me/chat", json={"message": "hi"}, headers=bob_headers)

    alice_call, bob_call = agent_client.calls
    assert alice_call["user_id"] == alice_id
    assert bob_call["user_id"] == bob_id
    # Each got their own fresh session — neither request carried the other's.
    assert alice_call["session_id"] is None
    assert bob_call["session_id"] is None
