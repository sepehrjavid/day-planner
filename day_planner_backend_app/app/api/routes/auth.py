"""Signup, login, logout, and (A6.4) password reset."""

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ...core import security
from ...core.config import Settings, get_settings
from ...db.models import EmailAlreadyRegistered, normalize_email
from ...db.store import Store
from ...schemas.auth import (
    LoginRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    SessionResponse,
    SignupRequest,
)
from ...services import email as email_service
from ..deps import bearer_token, get_store

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=SessionResponse, status_code=201)
async def signup(
    body: SignupRequest,
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings),
):
    try:
        security.validate_password_strength(body.password)
    except security.PasswordPolicyError as exc:
        # Literal 422 rather than the status constant: Starlette renamed
        # HTTP_422_UNPROCESSABLE_ENTITY to ..._CONTENT, so the constant warns
        # on new versions and doesn't exist on old ones.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        user_id = await store.create_user(
            email=body.email,
            password_hash=security.hash_password(body.password),
        )
    except EmailAlreadyRegistered:
        # This does leak that the address is registered. Signup can't really
        # avoid that without an email-verification round trip; /auth/login is
        # where enumeration resistance actually matters, and that path is
        # uniform.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="that email is already registered",
        ) from None

    token, expires_at = await store.create_session(
        user_id=user_id, ttl_seconds=settings.session_ttl_seconds
    )
    return SessionResponse(access_token=token, expires_at=expires_at, user_id=user_id)


@router.post("/login", response_model=SessionResponse)
async def login(
    body: LoginRequest,
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings),
):
    throttle = await store.check_login_throttle(
        body.email,
        max_attempts=settings.login_max_attempts,
        lockout_seconds=settings.login_lockout_seconds,
    )
    if throttle.locked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many failed attempts; try again later",
            headers={"Retry-After": str(throttle.retry_after_seconds)},
        )

    user = await store.get_user_by_email(body.email)
    # verify_password runs its dummy comparison when user is None, so an
    # unregistered address costs the same time as a wrong password.
    valid, needs_rehash = security.verify_password(
        (user or {}).get("password_hash"), body.password
    )

    if not valid:
        await store.record_login_failure(
            body.email,
            max_attempts=settings.login_max_attempts,
            lockout_seconds=settings.login_lockout_seconds,
        )
        # Identical response whether the account exists or the password is
        # wrong — otherwise this endpoint is a membership oracle.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid email or password",
        )

    assert user is not None
    if needs_rehash:
        # Argon2 parameters were raised since this user last logged in; upgrade
        # transparently now that we hold the plaintext.
        await store.update_password_hash(
            user_id=user["user_id"],
            password_hash=security.hash_password(body.password),
        )

    await store.clear_login_failures(body.email)
    token, expires_at = await store.create_session(
        user_id=user["user_id"], ttl_seconds=settings.session_ttl_seconds
    )
    return SessionResponse(
        access_token=token, expires_at=expires_at, user_id=user["user_id"]
    )


@router.post("/logout", status_code=204)
async def logout(
    token: str = Depends(bearer_token), store: Store = Depends(get_store)
):
    """Deleting the session server-side is what makes logout real — the reason
    these are opaque tokens rather than JWTs."""
    await store.delete_session(token)


def _client_ip(request: Request) -> str:
    """Best-effort caller IP for password-reset throttling only — never
    used for anything identity-bearing. Cloud Run's front end sets
    X-Forwarded-For with the original client IP as the first entry;
    request.client.host on Cloud Run reflects Google's own fronting
    proxy, not the caller, so it's unusable there and is only the
    local-dev fallback here."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/password-reset/request", status_code=status.HTTP_202_ACCEPTED)
async def request_password_reset(
    body: PasswordResetRequest,
    request: Request,
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings),
) -> None:
    """Always responds identically — 202, empty body — regardless of
    whether the address is registered or the request was rate-limited.
    A differing response on any of those axes is exactly what turns this
    into an account-enumeration oracle (A6.4's own acceptance criterion).
    Rate limiting is checked and recorded but never surfaced: a
    locked-out caller sees the same 202 a fresh one does and simply never
    receives an email, rather than a 429 that would leak which addresses
    are being actively targeted.

    Known gap, recorded rather than left silently unhandled: this
    equalises the *response*, not the *timing*. The SendGrid call itself
    is fire-and-forget (email_service.schedule_password_reset_email) so
    it no longer sits on the request path at all — both branches return
    right after their last Firestore call — but a registered address
    still costs one extra Firestore write (create_password_reset_token)
    that a non-existent one doesn't, so a sufficiently precise timing
    measurement could still distinguish the two. Login's dummy-hash
    comparison solves the equivalent problem there because Argon2
    verification time dominates; there's no equally cheap constant-time
    trick for a single extra document write, and closing that last gap
    (e.g. always writing a throwaway document) was judged not worth the
    complexity for what this product protects.
    """
    email_key = f"email:{normalize_email(body.email)}"
    ip_key = f"ip:{_client_ip(request)}"

    email_throttle = await store.check_reset_throttle(
        email_key,
        max_attempts=settings.password_reset_max_attempts,
        lockout_seconds=settings.password_reset_lockout_seconds,
    )
    ip_throttle = await store.check_reset_throttle(
        ip_key,
        max_attempts=settings.password_reset_max_attempts,
        lockout_seconds=settings.password_reset_lockout_seconds,
    )
    if email_throttle.locked or ip_throttle.locked:
        return

    await store.record_reset_attempt(
        email_key,
        max_attempts=settings.password_reset_max_attempts,
        lockout_seconds=settings.password_reset_lockout_seconds,
    )
    await store.record_reset_attempt(
        ip_key,
        max_attempts=settings.password_reset_max_attempts,
        lockout_seconds=settings.password_reset_lockout_seconds,
    )

    user = await store.get_user_by_email(body.email)
    if user is None:
        return

    token, _ = await store.create_password_reset_token(
        user_id=user["user_id"], ttl_seconds=settings.password_reset_ttl_seconds
    )
    # There is no /reset-password page yet — A6.4 explicitly scopes out
    # any UI. The token is embedded as a query param so a future frontend
    # can read it with no change needed on this side.
    reset_url = f"{settings.public_base_url.rstrip('/')}/reset-password?token={token}"

    email_service.schedule_password_reset_email(
        settings=settings, to_email=user["email"], reset_url=reset_url
    )


@router.post("/password-reset/confirm", status_code=204)
async def confirm_password_reset(
    body: PasswordResetConfirmRequest, store: Store = Depends(get_store)
) -> None:
    """Consumes the token atomically — single-use, so a captured or
    logged reset link can never be replayed — and, on success, evicts
    every session for the account, including whichever one made this
    request. A reset means "I lost control of this account"; unlike
    /me/password's change flow, there is no session to preserve.

    Password strength is validated before the token is consumed, so a
    request that fails policy doesn't burn the caller's only link.
    """
    try:
        security.validate_password_strength(body.new_password)
    except security.PasswordPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    user_id = await store.consume_password_reset_token(body.token)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired reset token",
        )

    await store.update_password_hash(
        user_id=user_id, password_hash=security.hash_password(body.new_password)
    )
    await store.delete_sessions(user_id=user_id)
