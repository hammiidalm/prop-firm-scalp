"""FastAPI application factory.

Creates a lightweight dashboard API that exposes:
* ``/health``          - liveness probe for Docker / systemd
* ``/ready``           - readiness probe (broker connected + WS alive)
* ``/api/v1/status``   - engine runtime state
* ``/api/v1/risk``     - risk manager snapshot
* ``/api/v1/trades``   - recent trade journal entries
* ``/api/v1/stats``    - session / symbol stats + equity curve
* ``/api/v1/pending``  - semi-auto pending signals (confirm/cancel)

The app is mounted as a background task inside the main engine; it does not
own the event loop. Dependencies (risk manager, executor, journal) are
injected via ``app.state`` at startup.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from app.api.routes import health, risk, settings, stats, status, trades
from app.utils.logging import get_logger

log = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):  # noqa: ANN201 - FastAPI typing
    log.info("dashboard api starting")
    yield
    log.info("dashboard api shutting down")


def create_app(
    *,
    risk_manager: Any = None,
    executor: Any = None,
    journal: Any = None,
    stats_aggregator: Any = None,
    ws_client: Any = None,
    engine: Any = None,
) -> FastAPI:
    """Build the FastAPI instance with injected dependencies."""
    app = FastAPI(
        title="prop-firm-scalp dashboard",
        version="0.1.0",
        default_response_class=ORJSONResponse,
        lifespan=_lifespan,
    )

    # CORS - allow local dev dashboards
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Inject dependencies onto app.state so routes can access them
    app.state.risk_manager = risk_manager
    app.state.executor = executor
    app.state.journal = journal
    app.state.stats_aggregator = stats_aggregator
    app.state.ws_client = ws_client
    app.state.engine = engine

    # Register routers
    app.include_router(health.router)
    app.include_router(risk.router, prefix="/api/v1")
    app.include_router(trades.router, prefix="/api/v1")
    app.include_router(stats.router, prefix="/api/v1")
    app.include_router(status.router, prefix="/api/v1")
    app.include_router(settings.router, prefix="/api/v1")

    return app
