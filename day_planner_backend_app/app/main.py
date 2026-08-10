"""Application entry point — the app-facing service.

Owns the half of OAuth that Vertex AI Agent Engine structurally cannot do: the
browser redirect. Agent Engine is invoke-only and has no public HTTP surface,
so consent and code exchange happen here; credential storage and everything
machine-to-machine (/internal/*) lives in the sibling
day_planner_backend_internal service — a fully separate deployable, not a
runtime mode of this one. See ../docs/oauth-design.md for why they're split.

This service's own public exposure is gated by var.publicly_exposed in
terraform/cloud_run.tf — closed during the test phase, one flag flip to open
at launch. This module only wires things together — routes live in
app/api/routes, the logic behind them in app/services.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api.router import router as api_router
from .core.config import get_settings
from .db.store import Store
from .providers import supported_providers
from .services.agent_client import AgentClient

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
    # AgentClient defers its own network call the same way Store defers its
    # Firestore client — see AgentClient._get_app.
    app.state.agent_client = AgentClient(
        project_id=settings.gcp_project_id,
        location=settings.agent_engine_location,
        reasoning_engine=settings.agent_engine_name,
    )
    logger.info("providers=%s", supported_providers(settings))
    yield


def create_app() -> FastAPI:
    application = FastAPI(
        title="Day Planner Backend (app)",
        lifespan=lifespan,
        # No interactive docs in production: this service's schema describes
        # credential-adjacent endpoints and there's nobody here to read it.
        docs_url=None,
        redoc_url=None,
    )
    application.include_router(api_router)
    return application


app = create_app()
