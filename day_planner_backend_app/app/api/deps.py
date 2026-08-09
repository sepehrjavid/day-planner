"""Shared FastAPI dependencies.

This is where identity gets established, and it's the most security-sensitive
file in the app. Each route group has exactly one legitimate source of
`user_id`:

  /me/*   -> current_user_id, from the session token
  /auth/* -> the single-use OAuth nonce

Mixing them up — reading a user_id from the body on a /me route, say — is how
one user ends up reading another's calendar.

There is no /internal/* here and no service-to-service auth dependency —
that entire surface lives in the sibling day_planner_backend_internal
service, a separate deployable, not a route this codebase can mount.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, status

from ..core.config import Settings, get_settings
from ..db.store import Store
from ..providers import OAuthProvider, get_provider
from ..services.agent_client import AgentClient


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_agent_client(request: Request) -> AgentClient:
    return request.app.state.agent_client


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
