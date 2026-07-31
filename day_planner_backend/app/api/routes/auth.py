"""Signup, login, logout."""

from fastapi import APIRouter, Depends, HTTPException, status

from ...core import security
from ...core.config import Settings, get_settings
from ...db.models import EmailAlreadyRegistered
from ...db.store import Store
from ...schemas.auth import LoginRequest, SessionResponse, SignupRequest
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
