"""Coverage of /me/chat and /me/chat/reset.

Two things worth pinning down: the trust boundary (user_id and session_id
are never anything other than what current_user_id and
Store.get_agent_session resolve them to, regardless of what a client puts in
the request body — see app/services/agent_client.py and
app/api/routes/chat.py), and the session lifecycle (idle sessions get
archived and rolled over automatically; a user can also force it via
/me/chat/reset).
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import main


def _now():
    return datetime.now(timezone.utc)


class FakeAgentClient:
    """Records every call so tests can assert on user_id/session_id without
    a real Agent Engine deployment."""

    def __init__(self):
        self.calls: list[dict] = []
        self.archived: list[dict] = []
        self._next_session = 0

    async def send_message(self, *, user_id, session_id, message):
        self.calls.append(
            {"user_id": user_id, "session_id": session_id, "message": message}
        )
        if session_id is None:
            self._next_session += 1
            session_id = f"session-{self._next_session}"
        return session_id, f"echo: {message}"

    async def archive_session(self, *, user_id, session_id):
        self.archived.append({"user_id": user_id, "session_id": session_id})


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
    assert response.json() == {"reply": "echo: hi", "new_session": True}
    assert agent_client.calls[0]["session_id"] is None
    assert store.users[user_id]["agent_session_id"] == "session-1"
    assert store.users[user_id]["agent_session_last_active_at"] is not None


def test_second_message_reuses_the_persisted_session(anon_client, user, agent_client):
    user_id, headers = user

    first = anon_client.post("/me/chat", json={"message": "hi"}, headers=headers)
    second = anon_client.post("/me/chat", json={"message": "again"}, headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json()["new_session"] is True
    assert second.json()["new_session"] is False
    assert agent_client.calls[0]["session_id"] is None
    assert agent_client.calls[1]["session_id"] == "session-1"
    assert agent_client.archived == []


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


def test_idle_session_is_archived_and_replaced(anon_client, user, agent_client, store):
    user_id, headers = user
    anon_client.post("/me/chat", json={"message": "hi"}, headers=headers)
    assert store.users[user_id]["agent_session_id"] == "session-1"

    # Backdate last activity past the default 6h timeout (core/config.py).
    store.users[user_id]["agent_session_last_active_at"] = _now() - timedelta(hours=7)

    response = anon_client.post("/me/chat", json={"message": "again"}, headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["new_session"] is True
    assert agent_client.archived == [{"user_id": user_id, "session_id": "session-1"}]
    # The archived session is never handed back to send_message.
    assert agent_client.calls[1]["session_id"] is None
    assert store.users[user_id]["agent_session_id"] == "session-2"


def test_recent_session_is_reused_without_archiving(anon_client, user, agent_client, store):
    user_id, headers = user
    anon_client.post("/me/chat", json={"message": "hi"}, headers=headers)
    store.users[user_id]["agent_session_last_active_at"] = _now() - timedelta(hours=1)

    response = anon_client.post("/me/chat", json={"message": "again"}, headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["new_session"] is False
    assert agent_client.archived == []
    assert agent_client.calls[1]["session_id"] == "session-1"


def test_reset_archives_and_clears_the_session(anon_client, user, agent_client, store):
    user_id, headers = user
    anon_client.post("/me/chat", json={"message": "hi"}, headers=headers)

    response = anon_client.post("/me/chat/reset", headers=headers)

    assert response.status_code == 204
    assert agent_client.archived == [{"user_id": user_id, "session_id": "session-1"}]
    assert store.users[user_id]["agent_session_id"] is None

    # The next message starts clean rather than reusing the archived id.
    follow_up = anon_client.post("/me/chat", json={"message": "again"}, headers=headers)
    assert follow_up.json()["new_session"] is True
    assert agent_client.calls[-1]["session_id"] is None


def test_reset_with_no_session_is_a_noop(anon_client, user, agent_client):
    _, headers = user

    response = anon_client.post("/me/chat/reset", headers=headers)

    assert response.status_code == 204
    assert agent_client.archived == []


def test_reset_requires_auth(anon_client, agent_client):
    response = anon_client.post("/me/chat/reset")
    assert response.status_code == 401
