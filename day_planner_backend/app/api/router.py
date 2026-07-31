"""Aggregates every route module into one router for main.py to mount.

Order matters for the /auth prefix: `auth.router` declares the literal paths
(/auth/signup, /auth/login, /auth/logout) and `oauth.router` declares the
parameterised ones (/auth/{provider_name}/start). They differ in segment count
so they can't actually collide, but keeping literals first is the habit that
stops a future /auth/reset-password from being swallowed by {provider_name}.
"""

from fastapi import APIRouter

from .routes import auth, health, internal, me, oauth

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(oauth.router)
api_router.include_router(me.router)
api_router.include_router(internal.router)
