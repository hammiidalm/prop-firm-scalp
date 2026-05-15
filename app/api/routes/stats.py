"""Session statistics + equity curve endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import ORJSONResponse

router = APIRouter(tags=["stats"])


@router.get("/stats")
async def get_stats(request: Request) -> ORJSONResponse:
    """Return aggregated session/symbol stats and the equity curve."""
    agg = getattr(request.app.state, "stats_aggregator", None)
    if agg is None:
        return ORJSONResponse({"error": "stats not initialized"}, status_code=503)
    return ORJSONResponse(agg.summary())


@router.get("/stats/equity")
async def equity_curve(request: Request) -> ORJSONResponse:
    """Return the equity curve as a list of balance snapshots."""
    agg = getattr(request.app.state, "stats_aggregator", None)
    if agg is None:
        return ORJSONResponse({"curve": []})
    return ORJSONResponse({"curve": agg.equity_curve, "length": len(agg.equity_curve)})
