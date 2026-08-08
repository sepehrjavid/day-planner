"""Test fixtures.

Environment is populated at import time, before anything pulls in
`app.config` — Settings is lru_cached, so the first read wins.

Unlike the app service's tests, there's no live connect flow to drive here —
the OAuth exchange code doesn't exist in this codebase. Tests seed FakeStore
directly with account data instead, exactly the shape
day_planner_backend_app's `save_account` would have produced.
"""

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("GCP_PROJECT_ID", "test-proj")
os.environ.setdefault("KMS_KEY_NAME", "projects/p/locations/l/keyRings/r/cryptoKeys/k")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_ID", "abc.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_SECRET", "shh")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8080")
os.environ.setdefault("SELF_BASE_URL", "http://localhost:8081")
os.environ.setdefault(
    "INTERNAL_CALLER_SERVICE_ACCOUNTS", "agent@test.iam.gserviceaccount.com"
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.api.deps import require_internal_caller  # noqa: E402
from app.services import crypto  # noqa: E402
from app.db.models import (  # noqa: E402
    STATUS_ACTIVE,
    STATUS_NEEDS_REAUTH,
    Calendar,
    ConnectedAccount,
    OAuthState,
    account_id_for,
)


def _now():
    return datetime.now(timezone.utc)


class FakeStore:
    """In-memory stand-in for Firestore, mirroring the real document shape."""

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.states: dict[str, OAuthState] = {}
        self.accounts: dict[str, dict[str, dict]] = {}
        self._seq = 0

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    # --- users (test setup only — no signup/login exists in this service) ---
    def seed_user(self, user_id: str, *, default_account_id: str | None = None):
        self.users[user_id] = {"default_account_id": default_account_id}
        self.accounts.setdefault(user_id, {})

    async def get_user(self, user_id):
        user = self.users.get(user_id)
        return None if user is None else {"user_id": user_id, **user}

    # --- oauth state ---
    async def create_oauth_state(self, *, user_id, provider, code_verifier, ttl_seconds):
        state = OAuthState(
            nonce=self._next("nonce"),
            user_id=user_id,
            provider=provider,
            code_verifier=code_verifier,
            expires_at=_now() + timedelta(seconds=ttl_seconds),
        )
        self.states[state.nonce] = state
        return state

    # --- connected accounts ---
    def seed_account(
        self,
        *,
        user_id: str,
        provider: str = "google",
        provider_account_id: str = "sub-1",
        email: str | None = "me@gmail.com",
        encrypted_refresh_token: str | None = "enc(RT-1|{user_id})",
        status: str = STATUS_ACTIVE,
        scopes: list[str] | None = None,
        calendars: list[Calendar] | None = None,
        make_default: bool = True,
    ) -> str:
        """Directly seed an account, bypassing the connect flow (which
        doesn't exist in this codebase)."""
        account_id = account_id_for(provider, provider_account_id)
        if encrypted_refresh_token and "{user_id}" in encrypted_refresh_token:
            encrypted_refresh_token = encrypted_refresh_token.format(user_id=user_id)
        self.accounts.setdefault(user_id, {})[account_id] = {
            "provider": provider,
            "credential_type": "oauth2",
            "provider_account_id": provider_account_id,
            "email": email,
            "scopes": scopes or ["openid", "email"],
            "encrypted_refresh_token": encrypted_refresh_token,
            "kms_key_name": None,
            "status": status,
            "calendars": calendars or [],
            "last_error": None,
        }
        self.users.setdefault(user_id, {})
        if make_default and not self.users[user_id].get("default_account_id"):
            self.users[user_id]["default_account_id"] = account_id
        return account_id

    async def save_account(
        self,
        *,
        user_id,
        provider,
        credential_type,
        provider_account_id,
        email,
        encrypted_refresh_token,
        kms_key_name,
        scopes,
        calendars,
    ):
        account_id = account_id_for(provider, provider_account_id)
        self.accounts.setdefault(user_id, {})[account_id] = {
            "provider": provider,
            "credential_type": credential_type,
            "provider_account_id": provider_account_id,
            "email": email,
            "scopes": scopes,
            "encrypted_refresh_token": encrypted_refresh_token,
            "kms_key_name": kms_key_name,
            "status": STATUS_ACTIVE,
            "calendars": calendars,
            "last_error": None,
        }
        if not self.users.setdefault(user_id, {}).get("default_account_id"):
            self.users[user_id]["default_account_id"] = account_id
        return account_id

    def _to_account(self, account_id, data):
        return ConnectedAccount(
            account_id=account_id,
            provider=data["provider"],
            credential_type=data["credential_type"],
            provider_account_id=data["provider_account_id"],
            email=data.get("email"),
            status=data["status"],
            scopes=data.get("scopes", []),
            calendars=list(data.get("calendars", [])),
            encrypted_refresh_token=data.get("encrypted_refresh_token"),
            kms_key_name=data.get("kms_key_name"),
            last_error=data.get("last_error"),
        )

    async def list_accounts(self, user_id):
        return [
            self._to_account(aid, data)
            for aid, data in self.accounts.get(user_id, {}).items()
        ]

    async def get_account(self, *, user_id, account_id):
        data = self.accounts.get(user_id, {}).get(account_id)
        return None if data is None else self._to_account(account_id, data)

    async def mark_needs_reauth(self, *, user_id, account_id, reason):
        data = self.accounts[user_id][account_id]
        data.update(
            status=STATUS_NEEDS_REAUTH, encrypted_refresh_token=None, last_error=reason
        )

    async def delete_account(self, *, user_id, account_id):
        self.accounts.get(user_id, {}).pop(account_id, None)
        if self.users.get(user_id, {}).get("default_account_id") == account_id:
            remaining = list(self.accounts.get(user_id, {}))
            self.users[user_id]["default_account_id"] = (
                remaining[0] if remaining else None
            )


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def anon_client(store):
    """Client with no auth override — for testing the gate itself."""
    with TestClient(main.app) as client:
        # Must land *after* entering the context manager: that's what runs the
        # lifespan, which builds a real Firestore-backed Store and would
        # otherwise clobber the fake.
        main.app.state.store = store
        yield client


@pytest.fixture
def client(store):
    """Client that passes the internal-caller gate."""
    main.app.dependency_overrides[require_internal_caller] = lambda: "agent@test.iam"
    with TestClient(main.app) as c:
        main.app.state.store = store
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture
def fake_crypto(monkeypatch):
    """Reversible stand-in for KMS that also encodes the user binding, so a
    ciphertext decrypted under the wrong user_id is detectable — the same
    property KMS gives us via additional authenticated data."""

    async def encrypt(key, plaintext, user_id):
        return f"enc({plaintext}|{user_id})"

    async def decrypt(key, ciphertext, user_id):
        prefix, suffix = "enc(", f"|{user_id})"
        if not (ciphertext.startswith(prefix) and ciphertext.endswith(suffix)):
            raise AssertionError(f"ciphertext {ciphertext!r} not bound to {user_id!r}")
        return ciphertext[len(prefix) : -len(suffix)]

    monkeypatch.setattr(crypto, "encrypt", encrypt)
    monkeypatch.setattr(crypto, "decrypt", decrypt)
