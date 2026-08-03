"""Refresh-token encryption via Cloud KMS.

This encrypts the refresh token *directly* with a KMS symmetric key rather
than using envelope encryption. Envelope encryption exists to avoid KMS
round-trips on large or high-volume payloads; a refresh token is a few
hundred bytes and is read at most a handful of times per user per day, well
inside KMS's 64 KiB plaintext limit. Direct encryption means we don't hand-roll
an AEAD (and can't get AES-GCM nonce handling wrong), at the cost of one KMS
call per connect and one per token refresh.

`user_id` is passed as additional authenticated data, so a ciphertext lifted
from one user's document cannot be decrypted under another's.
"""

import base64

from google.cloud import kms

_client: kms.KeyManagementServiceAsyncClient | None = None


def _kms() -> kms.KeyManagementServiceAsyncClient:
    global _client
    if _client is None:
        _client = kms.KeyManagementServiceAsyncClient()
    return _client


async def encrypt(key_name: str, plaintext: str, user_id: str) -> str:
    """Encrypt a secret, returning base64 ciphertext safe to store in Firestore."""
    response = await _kms().encrypt(
        request={
            "name": key_name,
            "plaintext": plaintext.encode("utf-8"),
            "additional_authenticated_data": user_id.encode("utf-8"),
        }
    )
    return base64.b64encode(response.ciphertext).decode("ascii")


async def decrypt(key_name: str, ciphertext_b64: str, user_id: str) -> str:
    """Decrypt a secret previously produced by `encrypt` for the same user."""
    response = await _kms().decrypt(
        request={
            "name": key_name,
            "ciphertext": base64.b64decode(ciphertext_b64),
            "additional_authenticated_data": user_id.encode("utf-8"),
        }
    )
    return response.plaintext.decode("utf-8")
