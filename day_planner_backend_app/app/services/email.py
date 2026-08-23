"""Outbound email via SendGrid's Mail Send API (A6.4).

Password-reset links are the only email this codebase sends. Calls
SendGrid's HTTP API directly with httpx rather than adding the
`sendgrid` PyPI package as a dependency for one POST request — see
https://docs.sendgrid.com/api-reference/mail-send/mail-send for the
request shape this mirrors.

The API key needs only "Mail Send" access on a Restricted Access key,
never "Full Access" — see terraform/secrets.tf for how the key reaches
this service (mounted from Secret Manager, value set out of band, same
pattern as google_oauth_client_secret).

schedule_password_reset_email is fire-and-forget, same shape as
day_planner_agent/memory_tools.py's background Memory Bank writes
(A2.4): the caller (routes/auth.py's request_password_reset) returns
right after writing the reset token, not after this completes, since
awaiting the SendGrid network call inline was most of what made a
registered address measurably slower to respond to than an
unregistered one — see that route's own docstring for the rest of that
reasoning.
"""

import asyncio
import logging

import httpx

from ..core.config import Settings

_SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"
_TIMEOUT = 10.0

logger = logging.getLogger(__name__)

# A fire-and-forget asyncio.Task with no other strong reference can be
# garbage-collected mid-flight ("Task was destroyed but it is pending") —
# this set plus the done-callback in schedule_password_reset_email is
# asyncio's own documented idiom for avoiding that, same pattern
# memory_tools.py's _pending_writes uses.
_pending_sends: set[asyncio.Task] = set()


class SendEmailError(Exception):
    """SendGrid rejected or failed to deliver the message."""


def schedule_password_reset_email(
    *, settings: Settings, to_email: str, reset_url: str
) -> None:
    """Fire-and-forget wrapper around send_password_reset_email. Failure
    is only logged — by the time this runs, the HTTP response to the
    original request has already been sent, so there is nothing left to
    report it to."""

    async def _send_and_log() -> None:
        try:
            await send_password_reset_email(
                settings=settings, to_email=to_email, reset_url=reset_url
            )
        except SendEmailError:
            logger.exception("password reset email failed to send")

    task = asyncio.create_task(_send_and_log())
    _pending_sends.add(task)
    task.add_done_callback(_pending_sends.discard)


async def send_password_reset_email(
    *, settings: Settings, to_email: str, reset_url: str
) -> None:
    """Raises SendEmailError on any non-2xx response or transport
    failure. The caller (routes/auth.py's request_password_reset)
    deliberately swallows this rather than letting it change the HTTP
    response — a delivery failure must not be distinguishable from "no
    such account," or the endpoint becomes an enumeration oracle."""
    minutes = settings.password_reset_ttl_seconds // 60
    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": settings.password_reset_from_email, "name": "Day Planner"},
        "subject": "Reset your Day Planner password",
        "content": [
            {
                "type": "text/plain",
                "value": (
                    "Someone requested a password reset for your Day "
                    "Planner account.\n\n"
                    f"If this was you, use this link within {minutes} minutes:\n"
                    f"{reset_url}\n\n"
                    "If you didn't request this, you can safely ignore "
                    "this email — your password hasn't changed."
                ),
            }
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                _SENDGRID_URL,
                json=payload,
                headers={"Authorization": f"Bearer {settings.sendgrid_api_key}"},
            )
    except httpx.HTTPError as exc:
        raise SendEmailError(f"SendGrid request failed: {exc}") from exc

    if response.status_code >= 400:
        raise SendEmailError(
            f"SendGrid rejected the message: {response.status_code} {response.text}"
        )
