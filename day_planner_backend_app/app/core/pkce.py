"""PKCE (RFC 7636) verifier/challenge generation.

Not strictly required for a confidential client that holds a client secret,
but Google supports it, OAuth 2.1 assumes it, and it costs ~10 lines to close
the authorization-code-interception hole.
"""

import base64
import hashlib
import secrets


def new_code_verifier() -> str:
    # token_urlsafe(64) yields ~86 chars, inside RFC 7636's 43-128 range.
    return secrets.token_urlsafe(64)


def code_challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
