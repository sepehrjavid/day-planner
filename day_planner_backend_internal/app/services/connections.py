"""Agent-facing use cases: minting connect links, access tokens, disconnecting.

Errors are raised as domain exceptions rather than HTTPException — the routes
decide how to turn a failure into a 409 with a machine-readable reason.

No authorization_url/complete_connection here — the actual OAuth code
exchange happens in the sibling day_planner_backend_app service, which has
its own, larger copy of this module for that. This service never talks to
Google's authorization endpoint directly; it only refreshes and revokes
tokens for accounts that app already connected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..core import pkce
from ..core.config import Settings
from ..db.models import ConnectedAccount
from ..db.store import Store
from ..providers import NeedsReauth
from . import crypto

logger = logging.getLogger(__name__)


class NotConnected(Exception):
    """No usable account for this user."""


class ReauthRequired(Exception):
    """The stored grant is dead; the user has to reconnect."""


@dataclass(frozen=True)
class ConnectLink:
    connect_url: str
    expires_at: datetime


@dataclass(frozen=True)
class AccessToken:
    account_id: str
    access_token: str
    expires_at: datetime
    scopes: list[str]


async def mint_connect_link(
    *, store: Store, settings: Settings, user_id: str, provider: str
) -> ConnectLink:
    """Create a single-use connect link.

    Deliver it privately — a DM or an ephemeral Slack message. Anyone who can
    click this link attaches *their* calendar account to this user_id. The
    URL points at /auth/{provider}/start on the sibling app service —
    settings.public_base_url is that service's origin, not this one's.
    """
    state = await store.create_oauth_state(
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


async def mint_access_token(
    *,
    store: Store,
    settings: Settings,
    provider_for,
    user_id: str,
    account_id: str | None,
) -> AccessToken:
    """Mint a short-lived provider access token for one connected account.

    Centralising this is what keeps refresh tokens and the KMS key reachable
    from exactly one service — the agent runtime only ever holds a ~1 hour
    token. `provider_for` is a callable so this stays independent of how the
    registry is wired.
    """
    if account_id is None:
        user = await store.get_user(user_id)
        account_id = (user or {}).get("default_account_id")
        if not account_id:
            raise NotConnected()

    account = await store.get_account(user_id=user_id, account_id=account_id)
    if account is None or not account.encrypted_refresh_token:
        raise NotConnected(account_id)
    if not account.is_usable:
        raise ReauthRequired(account_id)

    provider = provider_for(account.provider)
    refresh_token = await crypto.decrypt(
        account.kms_key_name or settings.kms_key_name,
        account.encrypted_refresh_token,
        user_id,
    )

    try:
        tokens = await provider.refresh(refresh_token=refresh_token)
    except NeedsReauth as exc:
        await store.mark_needs_reauth(
            user_id=user_id, account_id=account_id, reason=str(exc)
        )
        raise ReauthRequired(account_id) from exc

    # Google normally omits refresh_token on a refresh response, but rotates it
    # in some flows. Persist it when it does, or the next call uses a dead one.
    if tokens.refresh_token and tokens.refresh_token != refresh_token:
        rotated = await crypto.encrypt(
            settings.kms_key_name, tokens.refresh_token, user_id
        )
        await _resave(store, user_id, account, rotated, tokens.scopes, settings)

    return AccessToken(
        account_id=account_id,
        access_token=tokens.access_token,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=tokens.expires_in),
        scopes=tokens.scopes or account.scopes,
    )


async def _resave(
    store: Store,
    user_id: str,
    account: ConnectedAccount,
    encrypted_refresh_token: str,
    scopes: list[str],
    settings: Settings,
) -> None:
    await store.save_account(
        user_id=user_id,
        provider=account.provider,
        credential_type=account.credential_type,
        provider_account_id=account.provider_account_id,
        email=account.email,
        encrypted_refresh_token=encrypted_refresh_token,
        kms_key_name=settings.kms_key_name,
        scopes=scopes or account.scopes,
        calendars=account.calendars,
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
    account = await store.get_account(user_id=user_id, account_id=account_id)
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

    await store.delete_account(user_id=user_id, account_id=account_id)
    return True
