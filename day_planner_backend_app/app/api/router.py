"""Aggregates every route this service serves.

Unlike the internal service (day_planner_backend_internal), there's no
surface switching here — this codebase only ever knows about health, auth,
oauth, me, chat, and habit-sessions. /internal/* isn't a route this process
could ever mount even by misconfiguration; it isn't in the codebase at all.
Habit, habit-session, zone, and sleep-schedule data lives in this
service's own Store as of A6.1 — habit_sessions.py's /me/habit-sessions/
status route calls it directly, no HTTP hop to the internal service
involved.

Order matters for the /auth prefix: `auth.router` declares the literal paths
(/auth/signup, /auth/login, /auth/logout) and `oauth.router` declares the
parameterised ones (/auth/{provider_name}/start). They differ in segment count
so they can't actually collide, but keeping literals first is the habit that
stops a future /auth/reset-password from being swallowed by {provider_name}.
"""

from fastapi import APIRouter

from .routes import auth, chat, habit_sessions, health, me, oauth

router = APIRouter()
router.include_router(health.router)
router.include_router(auth.router)
router.include_router(oauth.router)
router.include_router(me.router)
router.include_router(chat.router)
router.include_router(habit_sessions.router)
