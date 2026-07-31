"""Google OAuth 2.0 + Calendar provider.

The authorization URL and token exchange are built by hand against Google's
documented endpoints rather than going through google-auth-oauthlib's `Flow`.
`Flow`'s PKCE surface has shifted between releases (it generates and stores
the verifier internally, which is exactly what we can't do when the verifier
has to survive a browser redirect in Firestore), so the explicit version is
both shorter and more stable across upgrades.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode

import httpx
from google.auth.transport import requests as ga_requests
from google.oauth2 import id_token as google_id_token

from .base import (
    AccountIdentity,
    CalendarRef,
    NeedsReauth,
    OAuthError,
    OAuthProvider,
    TokenSet,
)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
CALENDAR_LIST_ENDPOINT = "https://www.googleapis.com/calendar/v3/users/me/calendarList"

# Requested up front rather than incrementally. The agent's remit includes
# writing events (add_calendar_event in the spec), and a second consent screen
# mid-conversation is worse UX than one slightly broader screen at connect
# time. `calendar.readonly` is what permits calendarList lookups;
# `calendar.events` is what permits creating and updating events.
SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

_TIMEOUT = httpx.Timeout(10.0)


class GoogleCalendarProvider(OAuthProvider):
    name = "google"

    def __init__(self, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret

    def authorization_url(
        self, *, state: str, code_challenge: str, redirect_uri: str
    ) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # Without access_type=offline there is no refresh token, and the
            # 07:00 scheduled briefing — which runs with nobody present —
            # cannot work at all.
            "access_type": "offline",
            # Google only returns a refresh token on the *first* consent for a
            # given client/user pair. Forcing the prompt means a reconnect
            # after revocation actually yields a usable token instead of
            # silently returning an access token only.
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        return f"{AUTH_ENDPOINT}?{urlencode(params)}"

    async def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> tuple[TokenSet, AccountIdentity]:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": code_verifier,
                },
            )

        if response.status_code != 200:
            raise OAuthError(f"token exchange failed: {response.text}")

        payload = response.json()
        raw_id_token = payload.get("id_token")
        if not raw_id_token:
            raise OAuthError("token response contained no id_token")

        claims = await asyncio.to_thread(
            google_id_token.verify_oauth2_token,
            raw_id_token,
            ga_requests.Request(),
            self._client_id,
        )

        tokens = TokenSet(
            refresh_token=payload.get("refresh_token"),
            access_token=payload["access_token"],
            expires_in=int(payload.get("expires_in", 3600)),
            scopes=payload.get("scope", "").split(),
        )
        identity = AccountIdentity(
            provider_account_id=claims["sub"],
            email=claims.get("email"),
        )
        return tokens, identity

    async def refresh(self, *, refresh_token: str) -> TokenSet:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                TOKEN_ENDPOINT,
                data={
                    "refresh_token": refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "refresh_token",
                },
            )

        if response.status_code == 400:
            # invalid_grant covers every way a refresh token dies: the user
            # revoked access, six months of disuse, a password change, or
            # blowing past Google's ~100-live-tokens-per-user-per-client cap.
            # None of these are exceptional; they're a state to recover from.
            if response.json().get("error") == "invalid_grant":
                raise NeedsReauth("refresh token is no longer valid")
        if response.status_code != 200:
            raise OAuthError(f"token refresh failed: {response.text}")

        payload = response.json()
        return TokenSet(
            # Google usually omits this on refresh; the caller keeps the old one.
            refresh_token=payload.get("refresh_token"),
            access_token=payload["access_token"],
            expires_in=int(payload.get("expires_in", 3600)),
            scopes=payload.get("scope", "").split(),
        )

    async def revoke(self, *, refresh_token: str) -> None:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                REVOKE_ENDPOINT, data={"token": refresh_token}
            )
        # An already-dead token 400s, which is the outcome we wanted anyway.
        if response.status_code not in (200, 400):
            raise OAuthError(f"revocation failed: {response.text}")

    async def list_calendars(self, *, access_token: str) -> list[CalendarRef]:
        calendars: list[CalendarRef] = []
        page_token: str | None = None

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            while True:
                response = await client.get(
                    CALENDAR_LIST_ENDPOINT,
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={"pageToken": page_token} if page_token else None,
                )
                if response.status_code != 200:
                    raise OAuthError(f"could not list calendars: {response.text}")

                payload = response.json()
                for item in payload.get("items", []):
                    is_primary = bool(item.get("primary"))
                    role = item.get("accessRole", "")
                    calendars.append(
                        CalendarRef(
                            calendar_id=item["id"],
                            summary=item.get("summaryOverride") or item.get("summary"),
                            is_primary=is_primary,
                            # Subscribed feeds ("Holidays in Sweden", a shared
                            # read-only team calendar) come back with a reader
                            # role. They're rarely what someone means by "my
                            # schedule", so they're off unless chosen.
                            selected_by_default=is_primary
                            or role in ("owner", "writer"),
                        )
                    )

                page_token = payload.get("nextPageToken")
                if not page_token:
                    break

        if not calendars:
            raise OAuthError("account has no calendars")
        return calendars
