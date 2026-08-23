"""Shared OIDC-authenticated HTTP client machinery, used by both
backend_client.py (day_planner_backend_internal — credentials) and
domain_client.py (day_planner_backend_app's /agent/* — habits, habit
sessions, zones, sleep schedule), split apart by A6.2 since the agent
now talks to two services with two different base URLs and audiences.

This module is the one exception to this codebase's usual rule against
sharing code across deployables — day_planner_backend_app and
day_planner_backend_internal deliberately duplicate rather than share
(see each service's own store.py/security.py docstrings), because they
are two separate deployables with two separate dependency graphs.
backend_client.py and domain_client.py are not separate deployables —
they're two modules in the same day_planner_agent process — so
duplicating A2.1's caching/pooling machinery (concurrency-sensitive
double-checked locking, twice) would only add a second place for the
same bug to hide, for no isolation benefit. Each of the two callers
still gets its own token cache, its own pooled connection, and its own
audience — nothing is shared *between requests*, only the class that
implements the pattern is shared *in source*.
"""

from __future__ import annotations

import asyncio
import time

import google.auth.exceptions
import httpx
from google.auth.transport.requests import Request
from google.oauth2 import id_token as google_id_token

# A2.6: every call through a ServiceClient can fail this way — a
# transient or hard backend outage, a network blip, or the OIDC token
# mint itself failing (metadata server unreachable, credentials
# misconfigured). Callers should catch this tuple (alongside NeedsAuth,
# which is an expected, actionable state, not a failure) and report
# {"status": "error", ...} rather than letting it propagate and crash
# the turn — see zone_tools.py, habit_tools.py, and calendar_tool.py's
# backend_client/domain_client call sites for the pattern. httpx.HTTPError
# covers both connection/timeout failures (httpx.RequestError) and
# non-2xx responses (httpx.HTTPStatusError, from raise_for_status()
# below); GoogleAuthError covers the token mint failing.
BACKEND_ERROR = (httpx.HTTPError, google.auth.exceptions.GoogleAuthError)

_TIMEOUT = httpx.Timeout(10.0)

# OIDC ID tokens minted this way are valid ~1 hour; refresh a few minutes
# early so a token that's about to expire is never handed to a request
# that might still be in flight when it does.
_TOKEN_TTL_SECONDS = 55 * 60


class ServiceClient:
    """One OIDC-authenticated, connection-pooled client to one backend
    service — construct one per audience (see backend_client.py and
    domain_client.py's own module-level instances), never share one
    instance across two different base URLs.

    Both the token and the HTTP client are cached on the instance and
    reused across calls (A2.1) — a planning turn makes roughly 28
    backend calls, and minting a fresh token plus a fresh TLS handshake
    for every single one was the single highest-payoff fix in the
    roadmap this came from. get/post retry exactly once on a 401 (A2.3)
    to recover from the cached token going stale before its TTL says it
    should.
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token: str | None = None
        self._token_minted_at: float = 0.0
        self._token_lock = asyncio.Lock()
        self._http_client: httpx.AsyncClient | None = None
        self._http_client_lock = asyncio.Lock()

    async def _mint_id_token(self) -> str:
        # google-auth's fetch_id_token is synchronous (blocking network I/O);
        # every ADK tool in this codebase is async, so this always runs off
        # the event loop rather than stalling it.
        return await asyncio.to_thread(
            google_id_token.fetch_id_token, Request(), self._base_url
        )

    async def _get_id_token(self) -> str:
        """Cached and refreshed ~5 minutes before its actual ~1-hour expiry.

        Double-checked locking, mirroring day_planner_backend_app's
        agent_client.py -> _get_app: the cheap unlocked check handles the
        overwhelmingly common case (token already fresh) without ever
        touching the lock; the lock only matters for the rare race where
        several concurrent calls all see an expired/missing token at once
        and would otherwise each mint their own.
        """
        if self._token is None or (
            time.monotonic() - self._token_minted_at
        ) >= _TOKEN_TTL_SECONDS:
            async with self._token_lock:
                if self._token is None or (
                    time.monotonic() - self._token_minted_at
                ) >= _TOKEN_TTL_SECONDS:
                    self._token = await self._mint_id_token()
                    self._token_minted_at = time.monotonic()
        return self._token

    async def _get_client(self) -> httpx.AsyncClient:
        """Built lazily on first use, not at construction, and held for
        the instance's whole lifetime rather than one-per-call —
        connections get pooled and reused instead of a fresh TLS
        handshake on every one of a turn's backend calls. The
        Authorization header is deliberately *not* set here: it rotates
        roughly every 55 minutes (see _get_id_token) and this client
        outlives many such rotations, so it's attached per-request via
        _auth_headers instead."""
        if self._http_client is None:
            async with self._http_client_lock:
                if self._http_client is None:
                    self._http_client = httpx.AsyncClient(
                        base_url=self._base_url, timeout=_TIMEOUT
                    )
        return self._http_client

    async def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._get_id_token()}"}

    def _invalidate_token(self) -> None:
        self._token = None

    async def get(self, url: str, **kwargs) -> httpx.Response:
        """GET through the shared client, with A2.3's 401-retry-once: the
        cached token (A2.1) can go stale before its TTL — a service-account
        change, clock skew, an audience mismatch. Both /internal/* and
        /agent/* gate at the router level, ahead of any handler body, so a
        401 means nothing was processed; retrying is safe for reads and
        writes alike. A second 401 is a real auth failure, not staleness,
        and is not retried again here."""
        client = await self._get_client()
        response = await client.get(url, headers=await self._auth_headers(), **kwargs)
        if response.status_code == 401:
            self._invalidate_token()
            response = await client.get(url, headers=await self._auth_headers(), **kwargs)
        return response

    async def post(self, url: str, **kwargs) -> httpx.Response:
        """POST counterpart to get — see its docstring for the 401 handling."""
        client = await self._get_client()
        response = await client.post(url, headers=await self._auth_headers(), **kwargs)
        if response.status_code == 401:
            self._invalidate_token()
            response = await client.post(url, headers=await self._auth_headers(), **kwargs)
        return response
