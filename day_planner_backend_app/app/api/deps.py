"""Shared FastAPI dependencies.

This is where identity gets established, and it's the most security-sensitive
file in the app. Each route group has exactly one legitimate source of
`user_id`:

  /me/*    -> current_user_id, from the session token
  /agent/* -> require_agent_caller verifies the OIDC caller identity;
              user_id then comes from the request body, trusted because
              the caller itself is (A6.2) — never from anything the
              model produced (that trust rests on
              tool_context.session.user_id on the agent side).
  /auth/*  -> the single-use OAuth nonce

Mixing them up — reading a user_id from the body on a /me route, or
trusting a session token on /agent/* — is how one user ends up reading
another's calendar, or a signed-in browser session reaching a route
meant only for the agent's own service identity. current_user_id and
require_agent_caller must never be combined on the same router.

There is no /internal/* here — that surface lives in the sibling
day_planner_backend_internal service, a separate deployable, not a
route this codebase can mount.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, status

from ..core.config import Settings, get_settings
from ..core.security import AgentTokenError, verify_agent_token
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


async def require_agent_caller(
    token: str = Depends(bearer_token), settings: Settings = Depends(get_settings)
) -> str:
    """Gate for every route on the /agent router (A6.2).

    Audience is this service's own public URL (public_base_url) — a
    caller's OIDC token is audienced to whatever it's actually invoking,
    which is this service, never day_planner_backend_internal. Deliberately
    independent of current_user_id above: /agent/* never resolves a
    session token, and /me/* never verifies an OIDC identity — see this
    module's own docstring for why that separation is the whole point.
    """
    try:
        return await verify_agent_token(
            token,
            audience=settings.public_base_url,
            allowed_service_accounts=settings.agent_callers,
        )
    except AgentTokenError as exc:
        if not settings.agent_callers:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="agent auth is not configured",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
