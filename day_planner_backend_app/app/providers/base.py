"""Provider abstraction for calendar account connections.

Everything provider-specific about the OAuth dance lives behind this
interface so that adding Microsoft/Graph later is a new subclass rather
than a new code path through the routes.

Note the deliberate split between `OAuthProvider` (authorization-code flow
with refresh tokens) and the credential_type discriminator stored on the
user document. Apple/iCloud is *not* an OAuth provider — Sign in with Apple
grants identity only, and iCloud Calendar is CalDAV with an app-specific
password. When that gets added it will implement a different base class and
write `credential_type: "caldav_basic"`, while the storage layer, the user
document shape, and the tool-side lookup all stay as they are.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


class OAuthError(Exception):
    """Raised when a provider rejects an authorization or refresh request."""


class NeedsReauth(OAuthError):
    """The stored refresh token is no longer usable; the user must reconnect."""


@dataclass(frozen=True)
class TokenSet:
    refresh_token: str | None
    access_token: str
    expires_in: int
    scopes: list[str]


@dataclass(frozen=True)
class AccountIdentity:
    """Who the user consented as, on the provider's side."""

    provider_account_id: str  # stable opaque id (OIDC `sub`), never the email
    email: str | None


@dataclass(frozen=True)
class CalendarRef:
    calendar_id: str
    summary: str | None
    is_primary: bool
    # Whether to include this calendar in planning by default. A user's own
    # calendars are useful; the auto-subscribed "Holidays in Sweden" feed is
    # noise in a day plan, so read-only subscriptions start off.
    selected_by_default: bool = True


class OAuthProvider(abc.ABC):
    name: str
    credential_type: str = "oauth2"

    @abc.abstractmethod
    def authorization_url(
        self, *, state: str, code_challenge: str, redirect_uri: str
    ) -> str:
        """Build the URL to send the user's browser to for consent."""

    @abc.abstractmethod
    async def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> tuple[TokenSet, AccountIdentity]:
        """Trade an authorization code for tokens plus the consenting identity."""

    @abc.abstractmethod
    async def refresh(self, *, refresh_token: str) -> TokenSet:
        """Mint a new access token. Raises NeedsReauth if the grant is dead."""

    @abc.abstractmethod
    async def revoke(self, *, refresh_token: str) -> None:
        """Revoke the grant on the provider's side. Best effort."""

    @abc.abstractmethod
    async def list_calendars(self, *, access_token: str) -> list[CalendarRef]:
        """Every calendar on the account, so the user can choose which ones
        the planner should consider."""
