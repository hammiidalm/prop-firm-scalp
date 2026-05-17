"""Market data endpoints — current prices from REST poller."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import ORJSONResponse

router = APIRouter(tags=["market"])


@router.get("/market/prices")
async def get_prices(request: Request) -> ORJSONResponse:
    """Return latest known prices for all instruments from the REST poller."""
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return ORJSONResponse({"prices": {}})

    poller = getattr(engine, "_rest_poller", None)
    if poller is None:
        return ORJSONResponse({"prices": {}})

    return ORJSONResponse({"prices": poller.latest_prices})