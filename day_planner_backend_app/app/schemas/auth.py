"""Request/response models for signup, login, and sessions."""

from datetime import datetime

from pydantic import BaseModel, EmailStr


class SignupRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ChangePasswordRequest(BaseModel):
    # No length/policy validation on current_password here — it's checked
    # against the stored hash, not the policy; a too-short current_password
    # would already have been rejected at signup or a prior change.
    # new_password goes through the same validate_password_strength()
    # signup uses, at the route (A6.4).
    current_password: str
    new_password: str


class SessionResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user_id: str


class PasswordResetRequest(BaseModel):
    """Body for POST /auth/password-reset/request (A6.4). The route
    responds identically whether or not this address is registered — see
    that route's own docstring."""

    email: EmailStr


class PasswordResetConfirmRequest(BaseModel):
    """Body for POST /auth/password-reset/confirm (A6.4). No length
    validation on token — it's checked by whether it resolves to a live,
    unexpired document in db/store.py's password_resets collection, not
    by shape."""

    token: str
    new_password: str
