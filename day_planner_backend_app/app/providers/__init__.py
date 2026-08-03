"""Provider registry."""

from ..core.config import Settings
from .base import (
    AccountIdentity,
    CalendarRef,
    NeedsReauth,
    OAuthError,
    OAuthProvider,
    TokenSet,
)
from .google import GoogleCalendarProvider

__all__ = [
    "AccountIdentity",
    "CalendarRef",
    "NeedsReauth",
    "OAuthError",
    "OAuthProvider",
    "TokenSet",
    "get_provider",
    "supported_providers",
]


def _build(settings: Settings) -> dict[str, OAuthProvider]:
    return {
        "google": GoogleCalendarProvider(
            client_id=settings.google_oauth_client_id,
            client_secret=settings.google_oauth_client_secret,
        ),
    }


_cache: dict[str, OAuthProvider] | None = None


def _registry(settings: Settings) -> dict[str, OAuthProvider]:
    global _cache
    if _cache is None:
        _cache = _build(settings)
    return _cache


def supported_providers(settings: Settings) -> list[str]:
    return sorted(_registry(settings))


def get_provider(settings: Settings, name: str) -> OAuthProvider | None:
    return _registry(settings).get(name)
