"""Application entry point — the internal, agent-facing service.

Mints per-account access tokens, connect links, and calendar listings on
behalf of the day-planner agent. This is not a mode of the app-facing
service (day_planner_backend_app) — it's a fully separate deployable with
its own image and its own trimmed dependency set (no argon2-cffi: this
service never hashes a password). See ../docs/oauth-design.md for why: IAM
Conditions can't be combined with allUsers, so the only way to guarantee
/internal/* is never reachable by an anonymous browser is for it to live on
a service that is never granted allUsers, full stop — not a route behind a
flag that a misconfiguration could flip.

This module only wires things together — routes live in app/api/routes, the
logic behind them in app/services.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api.router import router as api_router
from .core.config import get_settings
from .db.store import Store
from .providers import supported_providers

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Store defers building its Firestore client until first use, so nothing
    # here depends on credential resolution succeeding — a metadata-server
    # blip must not take down the container before /healthz can answer.
    app.state.store = Store(
        project_id=settings.gcp_project_id, database=settings.firestore_database
    )
    logger.info("providers=%s", supported_providers(settings))
    yield


def create_app() -> FastAPI:
    application = FastAPI(
        title="Day Planner Backend (internal)",
        lifespan=lifespan,
        # No interactive docs in production: this service's schema describes
        # credential-adjacent endpoints and there's nobody here to read it.
        docs_url=None,
        redoc_url=None,
    )
    application.include_router(api_router)
    return application


app = create_app()
