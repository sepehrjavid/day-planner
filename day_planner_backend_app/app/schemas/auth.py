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
