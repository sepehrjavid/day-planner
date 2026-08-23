"""Connected calendar accounts (users/{user_id}/connected_accounts/{acct_id}).

save/delete also touch the parent users/{user_id} document's
default_account_id field — a genuine cross-collection dependency that
predates this split (A6.5 is pure restructuring; it relocates this
behavior, it doesn't remove the coupling). Both repositories share the
same Firestore client, so reaching into `users` from here is no
different at the database level than reaching into it from
UserRepository itself — the point of a separate class per collection
family is call-site visibility, not access control.

No mark_needs_reauth here, unlike day_planner_backend_internal's own
AccountRepository: this service never mints access tokens, so it can
never observe a refresh failure and has no reason to flip an account to
STATUS_NEEDS_REAUTH itself. save's healing behavior above (reconnecting
an account clears a prior needs_reauth state) still applies to whatever
that other service writes — this repository just isn't the writer.
"""

from __future__ import annotations

from google.cloud import firestore

from ..models import (
    STATUS_ACTIVE,
    Calendar,
    ConnectedAccount,
    account_id_for,
    utcnow,
)
from .users import USERS

CONNECTED_ACCOUNTS = "connected_accounts"


class AccountRepository:
    def __init__(self, db: firestore.AsyncClient) -> None:
        self._db = db

    def _accounts(self, user_id: str):
        return (
            self._db.collection(USERS).document(user_id).collection(CONNECTED_ACCOUNTS)
        )

    async def save(
        self,
        *,
        user_id: str,
        provider: str,
        credential_type: str,
        provider_account_id: str,
        email: str | None,
        encrypted_refresh_token: str,
        kms_key_name: str,
        scopes: list[str],
        calendars: list[Calendar],
    ) -> str:
        """Create or refresh a connected account. Returns its account_id.

        Reconnecting an account the user already linked lands on the same
        document, so it heals a needs_reauth account rather than growing a
        duplicate. Calendar *selection* survives a reconnect — the user's
        choice about which calendars matter shouldn't be silently reset
        because a refresh token expired.
        """
        account_id = account_id_for(provider, provider_account_id)
        ref = self._accounts(user_id).document(account_id)

        existing = await ref.get()
        is_new = not existing.exists
        previously_selected = (
            {
                c["calendar_id"]
                for c in (existing.to_dict() or {}).get("calendars", [])
                if c.get("selected")
            }
            if existing.exists
            else set()
        )

        merged = [
            Calendar(
                calendar_id=c.calendar_id,
                summary=c.summary,
                is_primary=c.is_primary,
                selected=(
                    c.calendar_id in previously_selected
                    if previously_selected
                    else c.selected
                ),
            )
            for c in calendars
        ]

        payload: dict = {
            "provider": provider,
            "credential_type": credential_type,
            "provider_account_id": provider_account_id,
            "email": email,
            "scopes": scopes,
            "encrypted_refresh_token": encrypted_refresh_token,
            "kms_key_name": kms_key_name,
            "status": STATUS_ACTIVE,
            "calendars": [c.to_dict() for c in merged],
            "last_error": None,
            "updated_at": utcnow(),
        }
        if is_new:
            payload["connected_at"] = utcnow()
        await ref.set(payload, merge=True)

        # First account connected becomes the default target for writes.
        user_ref = self._db.collection(USERS).document(user_id)
        snapshot = await user_ref.get()
        if not (snapshot.to_dict() or {}).get("default_account_id"):
            await user_ref.set(
                {"default_account_id": account_id, "updated_at": utcnow()}, merge=True
            )

        return account_id

    async def list(self, user_id: str) -> list[ConnectedAccount]:
        return [
            ConnectedAccount.from_dict(doc.id, doc.to_dict() or {})
            async for doc in self._accounts(user_id).stream()
        ]

    async def get(self, *, user_id: str, account_id: str) -> ConnectedAccount | None:
        snapshot = await self._accounts(user_id).document(account_id).get()
        if not snapshot.exists:
            return None
        return ConnectedAccount.from_dict(account_id, snapshot.to_dict() or {})

    async def set_calendar_selection(
        self, *, user_id: str, account_id: str, selected_calendar_ids: set[str]
    ) -> ConnectedAccount | None:
        ref = self._accounts(user_id).document(account_id)
        snapshot = await ref.get()
        if not snapshot.exists:
            return None

        data = snapshot.to_dict() or {}
        calendars = [
            {**c, "selected": c["calendar_id"] in selected_calendar_ids}
            for c in data.get("calendars", [])
        ]
        await ref.set({"calendars": calendars, "updated_at": utcnow()}, merge=True)
        return ConnectedAccount.from_dict(account_id, {**data, "calendars": calendars})

    async def delete(self, *, user_id: str, account_id: str) -> None:
        await self._accounts(user_id).document(account_id).delete()

        user_ref = self._db.collection(USERS).document(user_id)
        snapshot = await user_ref.get()
        if (snapshot.to_dict() or {}).get("default_account_id") != account_id:
            return

        # Promote whatever is left, so "default" doesn't dangle at a deleted id.
        remaining = await self.list(user_id)
        await user_ref.set(
            {
                "default_account_id": remaining[0].account_id if remaining else None,
                "updated_at": utcnow(),
            },
            merge=True,
        )
