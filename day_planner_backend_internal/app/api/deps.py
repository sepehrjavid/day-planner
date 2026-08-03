"""Shared FastAPI dependencies.

This is where identity gets established, and it's the most security-sensitive
file in the app. /internal/* takes `user_id` from the request body — the
caller is a trusted service acting on a user's behalf, and that trust rests
entirely on require_internal_caller below actually verifying who's asking.

There's no session-based identity here at all (no current_user_id, no
bearer-session flow) — that belongs to the app-facing service
(day_planner_backend_app), a different codebase entirely.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, status

from ..core.config import Settings, get_settings
from ..core.security import InternalTokenError, verify_internal_token
from ..db.store import Store
from ..providers import OAuthProvider, get_provider


def get_store(request: Request) -> Store:
    return request.app.state.store


def require_known_provider(
    name: str, settings: Settings = Depends(get_settings)
) -> OAuthProvider:
    """Providers named in a request body. Unknown ones are a 404, not a 500."""
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


async def require_internal_caller(
    token: str = Depends(bearer_token), settings: Settings = Depends(get_settings)
) -> str:
    """Gate for every route in this service.

    Audience is this service's own URL (self_base_url) — a caller's OIDC
    token is audienced to whatever it's actually invoking, which is this
    service, never the app-facing one.
    """
    try:
        return await verify_internal_token(
            token,
            audience=settings.self_base_url,
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
