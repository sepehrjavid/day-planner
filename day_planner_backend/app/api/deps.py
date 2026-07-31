"""Shared FastAPI dependencies.

This is where identity gets established, and it's the most security-sensitive
file in the app. Each route group has exactly one legitimate source of
`user_id`:

  /me/*        -> current_user_id, from the session token
  /internal/*  -> the request body, from an authenticated service caller
  /auth/*      -> the single-use OAuth nonce

Mixing them up — reading a user_id from the body on a /me route, say — is how
one user ends up reading another's calendar.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, status

from ..core.config import Settings, get_settings
from ..core.security import InternalTokenError, verify_internal_token
from ..db.store import Store
from ..providers import OAuthProvider, get_provider


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_provider_or_404(
    provider_name: str, settings: Settings = Depends(get_settings)
) -> OAuthProvider:
    """Resolve a path's {provider_name}. Unknown providers are a 404, not a 500."""
    provider = get_provider(settings, provider_name)
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown provider '{provider_name}'",
        )
    return provider


def require_known_provider(
    name: str, settings: Settings = Depends(get_settings)
) -> OAuthProvider:
    """Same check for providers named in a request body rather than the path."""
    provider = get_provider(settings, name)
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown provider '{name}'"
        )
    return provider


def provider_factory(settings: Settings = Depends(get_settings)):
    """Callable the service layer uses to resolve a stored account's provider."""

    def resolve(name: str) -> OAuthProvider:
        provider = get_provider(settings, name)
        if provider is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"stored account references unknown provider '{name}'",
            )
        return provider

    return resolve


def bearer_token(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return authorization.removeprefix("Bearer ").strip()


async def current_user_id(
    store: Store = Depends(get_store), token: str = Depends(bearer_token)
) -> str:
    """Resolve a session token to a user_id.

    Every /me route hangs off this. The user_id it returns is the *only*
    acceptable source of identity there — a user_id in a request body would let
    any logged-in user act on any other account.
    """
    user_id = await store.resolve_session(token)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired session",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_id


async def require_internal_caller(
    token: str = Depends(bearer_token), settings: Settings = Depends(get_settings)
) -> str:
    """Gate for /internal/*. Returns the verified caller's service account."""
    try:
        return await verify_internal_token(
            token,
            audience=settings.public_base_url,
            allowed_service_accounts=settings.internal_callers,
        )
    except InternalTokenError as exc:
        if not settings.internal_callers:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="internal auth is not configured",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
