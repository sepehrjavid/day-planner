"""Aggregates every route this service serves.

Unlike the app service (day_planner_backend_app), there's no surface
switching here — this codebase only ever knows about health and
/internal/*. Accounts, OAuth, and calendar-management routes aren't in this
codebase at all; there's no config flag that could accidentally expose them
here, because the code to serve them simply doesn't exist in this deployable.
"""

from fastapi import APIRouter

from .routes import health, internal

router = APIRouter()
router.include_router(health.router)
router.include_router(internal.router)
