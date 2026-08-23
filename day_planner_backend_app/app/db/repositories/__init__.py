"""Firestore persistence — one repository per collection family.

Collection layout:

  users/{user_id}                                the account: email, password
                                                 hash, default calendar account,
                                                 agent_session_id + agent_session
                                                 _last_active_at (which Agent
                                                 Engine session /me/chat resumes,
                                                 and whether it's gone idle),
                                                 quota_date + quota_count (the
                                                 daily /me/chat message quota —
                                                 see UserRepository
                                                 .check_and_consume_quota)

  users/{user_id}/connected_accounts/{acct_id}   one per linked calendar
                                                 account. A user can have as
                                                 many as they like — personal
                                                 Google, work Google, later a
                                                 CalDAV one. Each holds its own
                                                 credential and calendars.

  users/{user_id}/habits/{habit_id}              one per recurring goal the
                                                 agent tracks and schedules
                                                 (see db/models.py's Habit
                                                 docstring for why this is a
                                                 plain record and not part of
                                                 the Memory Bank profile).
                                                 Moved here from
                                                 day_planner_backend_internal
                                                 by A6.1 — this is ordinary
                                                 user data with no credential
                                                 exposure, unlike everything
                                                 else in this collection
                                                 layout's neighbourhood.

  users/{user_id}/habit_sessions/{session_id}    one per calendar event the
                                                 agent created for a habit,
                                                 keyed on (calendar_id,
                                                 event_id) via
                                                 habit_session_id_for — see
                                                 db/models.py's HabitSession
                                                 docstring. Outlives the
                                                 calendar event on purpose:
                                                 review_habit_week needs a
                                                 record of what was planned
                                                 even after the event itself
                                                 is deleted.

  users/{user_id}/zones/{zone_id}                one per named scheduling
                                                 restriction (work hours,
                                                 commute, ...) — see
                                                 db/models.py's Zone
                                                 docstring. No documents at
                                                 all means no restriction of
                                                 this kind exists for the
                                                 user.

  users/{user_id}/sleep_schedule/{fixed id}      singleton: the user's
                                                 sleep/wake times and the
                                                 cool-down/wake-up windows
                                                 derived from them — see
                                                 db/models.py's SleepSchedule
                                                 docstring. A subcollection
                                                 with one fixed-id document,
                                                 the same shape every other
                                                 user-scoped resource here
                                                 uses, rather than a bare
                                                 field on the user doc.

  user_emails/{normalized_email}                 uniqueness lock for signup.
                                                 Firestore has no unique
                                                 constraint, so "query, then
                                                 write" races two concurrent
                                                 signups into duplicate
                                                 accounts. A document keyed by
                                                 the email, claimed inside a
                                                 transaction, is the constraint.

  sessions/{token_hash}                          opaque login sessions, TTL'd
  login_throttle/{normalized_email}              failed-attempt counter
  oauth_states/{nonce}                           single-use connect links, TTL'd
  password_resets/{token_hash}                   opaque reset tokens, TTL'd (A6.4)
  password_reset_throttle/{email-or-ip key}      reset-request attempt counter (A6.4)

Two things are deliberately never stored: provider access tokens (they last an
hour; the refresh token can always mint another) and raw session tokens.

Why this is a package of repositories rather than one Store class (A6.5):
after A6.1 moved the planning domain in, Store had grown to 38 methods across
nine sections covering two genuinely separate concerns — identity/credential
state and planning domain data — with nothing stopping a habit method from
reaching into account state. Each repository below owns exactly one
collection family and takes its Firestore client explicitly through its own
constructor — no mixins, no shared base class, no attribute any repository
reads without declaring it. ../store.py is a thin facade exposing one
property per repository (`store.habits`, `store.zones`, `store.accounts`,
...), so `store.<repo>.<method>(...)` is the only call-site shape and
ownership is visible wherever it's used.

This is pure restructuring — no Firestore path, document shape, or method
semantics changed. A handful of decisions are non-obvious enough that they're
called out again at the exact method that implements them (not just here),
since a future edit is more likely to read the method than this module intro:
upsert_habit_session's merge-by-omission (HabitSessionRepository.upsert),
update_habit/update_zone returning None on an unknown id (HabitRepository
.update, ZoneRepository.update), set_sleep_schedule's wholesale day_overrides
replacement (SleepScheduleRepository.set), set_habit_session_status's
same-status no-op (HabitSessionRepository.set_status), and the two
transactional methods (UserRepository.create, UserRepository
.check_and_consume_quota).
"""

from .accounts import AccountRepository
from .habit_sessions import HabitSessionRepository
from .habits import HabitRepository
from .login_throttle import LoginThrottleRepository
from .oauth_states import OAuthStateRepository
from .password_reset_throttle import PasswordResetThrottleRepository
from .password_resets import PasswordResetRepository
from .sessions import SessionRepository
from .sleep_schedule import SleepScheduleRepository
from .users import UserRepository
from .zones import ZoneRepository

__all__ = [
    "AccountRepository",
    "HabitRepository",
    "HabitSessionRepository",
    "LoginThrottleRepository",
    "OAuthStateRepository",
    "PasswordResetRepository",
    "PasswordResetThrottleRepository",
    "SessionRepository",
    "SleepScheduleRepository",
    "UserRepository",
    "ZoneRepository",
]
