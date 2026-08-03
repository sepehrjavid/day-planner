"""Signup, login, sessions, and the /me authorization boundary."""

from app.core import security

from .conftest import GOOD_PASSWORD


def test_signup_returns_a_usable_session(anon_client):
    response = anon_client.post(
        "/auth/signup", json={"email": "new@example.com", "password": GOOD_PASSWORD}
    )
    assert response.status_code == 201
    token = response.json()["access_token"]

    me = anon_client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "new@example.com"


def test_password_is_never_stored_in_the_clear(anon_client, store):
    anon_client.post(
        "/auth/signup", json={"email": "new@example.com", "password": GOOD_PASSWORD}
    )
    stored = next(iter(store.users.values()))["password_hash"]
    assert GOOD_PASSWORD not in stored
    assert stored.startswith("$argon2id$")


def test_email_is_normalized(anon_client):
    anon_client.post(
        "/auth/signup", json={"email": "Me@Example.COM", "password": GOOD_PASSWORD}
    )
    # Same address, different casing — must not create a second account.
    duplicate = anon_client.post(
        "/auth/signup", json={"email": "me@example.com", "password": GOOD_PASSWORD}
    )
    assert duplicate.status_code == 409

    login = anon_client.post(
        "/auth/login", json={"email": "  ME@EXAMPLE.com ", "password": GOOD_PASSWORD}
    )
    assert login.status_code == 200


def test_short_passwords_are_rejected(anon_client):
    response = anon_client.post(
        "/auth/signup", json={"email": "new@example.com", "password": "short"}
    )
    assert response.status_code == 422


def test_absurdly_long_passwords_are_rejected(anon_client):
    """Argon2 is intentionally expensive; hashing an unbounded input is a
    cheap way to burn the server's CPU."""
    response = anon_client.post(
        "/auth/signup", json={"email": "new@example.com", "password": "x" * 5000}
    )
    assert response.status_code == 422


def test_login_with_wrong_password_fails(anon_client, user):
    response = anon_client.post(
        "/auth/login", json={"email": "me@example.com", "password": "wrong-password-1"}
    )
    assert response.status_code == 401


def test_login_does_not_leak_whether_an_account_exists(anon_client, user):
    """Unknown address and wrong password must be indistinguishable, or the
    endpoint is a membership oracle."""
    unknown = anon_client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": "whatever-123"}
    )
    wrong = anon_client.post(
        "/auth/login", json={"email": "me@example.com", "password": "whatever-123"}
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_repeated_failures_lock_the_account(anon_client, user):
    for _ in range(8):
        anon_client.post(
            "/auth/login", json={"email": "me@example.com", "password": "nope-nope-123"}
        )

    locked = anon_client.post(
        "/auth/login", json={"email": "me@example.com", "password": GOOD_PASSWORD}
    )
    assert locked.status_code == 429
    assert "Retry-After" in locked.headers


def test_successful_login_clears_the_failure_counter(anon_client, user, store):
    for _ in range(3):
        anon_client.post(
            "/auth/login", json={"email": "me@example.com", "password": "nope-nope-123"}
        )
    anon_client.post(
        "/auth/login", json={"email": "me@example.com", "password": GOOD_PASSWORD}
    )
    assert "me@example.com" not in store.throttle


def test_logout_revokes_the_session(anon_client, user):
    _, headers = user
    assert anon_client.post("/auth/logout", headers=headers).status_code == 204
    # The whole reason these are opaque tokens rather than JWTs.
    assert anon_client.get("/me", headers=headers).status_code == 401


def test_me_requires_a_session(anon_client):
    assert anon_client.get("/me").status_code == 401
    assert (
        anon_client.get("/me", headers={"Authorization": "Bearer nope"}).status_code
        == 401
    )


def test_expired_session_is_rejected(anon_client, user, store):
    """Firestore TTL deletion lags by hours, so expiry has to be enforced on
    read rather than trusted to the sweeper."""
    from datetime import datetime, timedelta, timezone

    _, headers = user
    token = headers["Authorization"].removeprefix("Bearer ")
    store.sessions[token]["expires_at"] = datetime.now(timezone.utc) - timedelta(
        seconds=1
    )
    assert anon_client.get("/me", headers=headers).status_code == 401


def test_argon2_rehash_on_parameter_upgrade(anon_client, user, store, monkeypatch):
    """Raising Argon2 cost later must upgrade existing users on next login,
    not leave them on the old parameters forever."""
    _, _ = user
    original = store.users["user-1"]["password_hash"]

    class AlwaysNeedsRehash:
        """PasswordHasher uses __slots__, so its methods can't be patched in
        place — swap the module-level hasher instead."""

        def __init__(self, inner):
            self._inner = inner

        def hash(self, password):
            return self._inner.hash(password)

        def verify(self, hashed, password):
            return self._inner.verify(hashed, password)

        def check_needs_rehash(self, hashed):
            return True

    monkeypatch.setattr(security, "_hasher", AlwaysNeedsRehash(security._hasher))
    assert (
        anon_client.post(
            "/auth/login", json={"email": "me@example.com", "password": GOOD_PASSWORD}
        ).status_code
        == 200
    )
    assert store.users["user-1"]["password_hash"] != original
