"""Calendar-connection use cases: linking an account and disconnecting it.

Everything the OAuth flow actually *does* lives here, so the route handlers
stay thin and this logic is reachable without an HTTP client. Errors are
raised as domain exceptions rather than HTTPException — the routes decide how
to present a failure (an HTML page for the browser-facing callback here; a
machine-readable 409 in the sibling day_planner_backend_internal service,
which has its own, smaller copy of this module for mint_access_token /
disconnect_account — this service never mints access tokens itself, that's
what /internal/* is for).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from ..core import pkce
from ..core.config import Settings
from ..db.models import Calendar
from ..db.store import Store
from ..providers import OAuthError, OAuthProvider
from . import crypto

logger = logging.getLogger(__name__)


class ConnectFailed(Exception):
    """Consent could not be completed. Carries a message safe to show a user."""


@dataclass(frozen=True)
class ConnectLink:
    connect_url: str
    expires_at: datetime


@dataclass(frozen=True)
class ConnectResult:
    account_id: str
    email: str | None
    calendars_found: int
    calendars_selected: int


async def mint_connect_link(
    *, store: Store, settings: Settings, user_id: str, provider: str
) -> ConnectLink:
    """Create a single-use connect link.

    Deliver it privately — a DM or an ephemeral Slack message. Anyone who can
    click this link attaches *their* calendar account to this user_id.
    """
    state = await store.oauth_states.create(
        user_id=user_id,
        provider=provider,
        code_verifier=pkce.new_code_verifier(),
        ttl_seconds=settings.state_ttl_seconds,
    )
    base = settings.public_base_url.rstrip("/")
    return ConnectLink(
        connect_url=f"{base}/auth/{provider}/start?s={state.nonce}",
        expires_at=state.expires_at,
    )


def authorization_url(
    *, provider: OAuthProvider, settings: Settings, state
) -> str:
    return provider.authorization_url(
        state=state.nonce,
        code_challenge=pkce.code_challenge_for(state.code_verifier),
        redirect_uri=settings.redirect_uri(provider.name),
    )


async def complete_connection(
    *,
    store: Store,
    settings: Settings,
    provider: OAuthProvider,
    code: str,
    state_nonce: str,
) -> ConnectResult:
    """Exchange the authorization code and persist the connected account."""
    oauth_state = await store.oauth_states.consume(state_nonce)
    if oauth_state is None or oauth_state.is_expired:
        raise ConnectFailed("That connect link has expired or already been used.")
    if oauth_state.provider != provider.name:
        raise ConnectFailed("That connect link was issued for a different provider.")

    try:
        tokens, identity = await provider.exchange_code(
            code=code,
            code_verifier=oauth_state.code_verifier,
            redirect_uri=settings.redirect_uri(provider.name),
        )
    except OAuthError as exc:
        logger.warning("code exchange failed for %s: %s", oauth_state.user_id, exc)
        raise ConnectFailed(
            "The provider rejected the sign-in. Please try again."
        ) from exc

    if not tokens.refresh_token:
        # We ask for access_type=offline and prompt=consent, so this shouldn't
        # happen — but without a refresh token the scheduled morning briefing
        # can never run, so fail loudly rather than storing a doomed record.
        logger.error("no refresh token returned for user %s", oauth_state.user_id)
        raise ConnectFailed(
            "The provider didn't grant offline access. Try removing this app "
            "from your account's third-party access list, then reconnect."
        )

    try:
        found = await provider.list_calendars(access_token=tokens.access_token)
    except OAuthError as exc:
        logger.warning("calendar listing failed: %s", exc)
        raise ConnectFailed(
            "Connected, but the calendar list couldn't be read."
        ) from exc

    encrypted = await crypto.encrypt(
        settings.kms_key_name, tokens.refresh_token, oauth_state.user_id
    )
    account_id = await store.accounts.save(
        user_id=oauth_state.user_id,
        provider=provider.name,
        credential_type=provider.credential_type,
        provider_account_id=identity.provider_account_id,
        email=identity.email,
        encrypted_refresh_token=encrypted,
        kms_key_name=settings.kms_key_name,
        scopes=tokens.scopes,
        calendars=[
            Calendar(
                calendar_id=c.calendar_id,
                summary=c.summary,
                is_primary=c.is_primary,
                selected=c.selected_by_default,
            )
            for c in found
        ],
    )
    logger.info(
        "connected %s account for user %s (%d calendars)",
        provider.name,
        oauth_state.user_id,
        len(found),
    )

    return ConnectResult(
        account_id=account_id,
        email=identity.email,
        calendars_found=len(found),
        calendars_selected=sum(1 for c in found if c.selected_by_default),
    )


async def disconnect_account(
    *,
    store: Store,
    settings: Settings,
    provider_for,
    user_id: str,
    account_id: str,
) -> bool:
    """Revoke the grant at the provider *and* delete our copy.

    Returns False if there was no such account. Deleting only our side would
    leave a live grant sitting in the user's Google account with nothing to
    show for it.
    """
    account = await store.accounts.get(user_id=user_id, account_id=account_id)
    if account is None:
        return False

    if account.encrypted_refresh_token:
        try:
            refresh_token = await crypto.decrypt(
                account.kms_key_name or settings.kms_key_name,
                account.encrypted_refresh_token,
                user_id,
            )
            await provider_for(account.provider).revoke(refresh_token=refresh_token)
        except Exception as exc:  # noqa: BLE001
            # Still drop our copy — a stuck credential we refuse to delete is
            # worse than one we couldn't revoke remotely.
            logger.warning("revocation failed for %s: %s", user_id, exc)

    await store.accounts.delete(user_id=user_id, account_id=account_id)
    return True
