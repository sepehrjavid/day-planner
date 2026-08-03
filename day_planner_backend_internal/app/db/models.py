"""Domain types and the small amount of key-derivation logic that goes with them.

Kept separate from `store.py` so the shapes can be imported — by services,
schemas, or tests — without dragging in the Firestore client.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

STATUS_ACTIVE = "active"
STATUS_NEEDS_REAUTH = "needs_reauth"


class EmailAlreadyRegistered(Exception):
    """Signup lost the race, or the address was already taken."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_session_token(token: str) -> str:
    """Sessions are stored as digests, so a database dump can't be replayed
    as a set of live logins."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def account_id_for(provider: str, provider_account_id: str) -> str:
    """Deterministic document ID for a connected account.

    Keying on (provider, provider_account_id) rather than an auto-ID is what
    makes "connect the same Google account twice" an idempotent update instead
    of a duplicate row — Firestore can't enforce a unique index, but it can
    enforce a document ID.
    """
    return f"{provider}__{provider_account_id}"


@dataclass(frozen=True)
class OAuthState:
    nonce: str
    user_id: str
    provider: str
    code_verifier: str
    expires_at: datetime

    @property
    def is_expired(self) -> bool:
        return utcnow() >= self.expires_at


@dataclass(frozen=True)
class ThrottleState:
    locked: bool
    retry_after_seconds: int = 0


@dataclass(frozen=True)
class Calendar:
    calendar_id: str
    summary: str | None
    is_primary: bool
    selected: bool

    def to_dict(self) -> dict:
        return {
            "calendar_id": self.calendar_id,
            "summary": self.summary,
            "is_primary": self.is_primary,
            "selected": self.selected,
        }

    @staticmethod
    def from_dict(data: dict) -> "Calendar":
        return Calendar(
            calendar_id=data["calendar_id"],
            summary=data.get("summary"),
            is_primary=bool(data.get("is_primary")),
            selected=bool(data.get("selected", True)),
        )


@dataclass(frozen=True)
class ConnectedAccount:
    account_id: str
    provider: str
    credential_type: str
    provider_account_id: str
    email: str | None
    status: str
    scopes: list[str] = field(default_factory=list)
    calendars: list[Calendar] = field(default_factory=list)
    encrypted_refresh_token: str | None = None
    kms_key_name: str | None = None
    last_error: str | None = None

    @property
    def is_usable(self) -> bool:
        return self.status == STATUS_ACTIVE and bool(self.encrypted_refresh_token)

    @staticmethod
    def from_dict(account_id: str, data: dict) -> "ConnectedAccount":
        return ConnectedAccount(
            account_id=account_id,
            provider=data["provider"],
            credential_type=data.get("credential_type", "oauth2"),
            provider_account_id=data["provider_account_id"],
            email=data.get("email"),
            status=data.get("status", STATUS_ACTIVE),
            scopes=data.get("scopes", []),
            calendars=[Calendar.from_dict(c) for c in data.get("calendars", [])],
            encrypted_refresh_token=data.get("encrypted_refresh_token"),
            kms_key_name=data.get("kms_key_name"),
            last_error=data.get("last_error"),
        )
