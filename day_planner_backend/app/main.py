"""Application entry point.

Owns the half of OAuth that Vertex AI Agent Engine structurally cannot do: the
browser redirect. Agent Engine is invoke-only and has no public HTTP surface,
so consent, code exchange, and credential storage happen here, and the agent
asks /internal/* for what it needs.

This module only wires things together — routes live in app/api/routes, the
logic behind them in app/services.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api.router import api_router
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
    logger.info("providers available: %s", supported_providers(settings))
    yield


def create_app() -> FastAPI:
    application = FastAPI(
        title="Day Planner Backend",
        lifespan=lifespan,
        # No interactive docs in production: this service's schema describes
        # credential-adjacent endpoints and there's nobody here to read it.
        docs_url=None,
        redoc_url=None,
    )
    application.include_router(api_router)
    return application


app = create_app()
