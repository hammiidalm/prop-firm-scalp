"""Risk-manager state endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import ORJSONResponse

router = APIRouter(tags=["risk"])


@router.get("/risk")
async def get_risk_state(request: Request) -> ORJSONResponse:
    """Return the current risk manager snapshot (balance, DD, trade counts)."""
    risk = getattr(request.app.state, "risk_manager", None)
    if risk is None:
        return ORJSONResponse({"error": "risk manager not initialized"}, status_code=503)
    return ORJSONResponse(risk.snapshot())


@router.post("/risk/disable")
async def disable_trading(request: Request) -> ORJSONResponse:
    """Emergency kill-switch: disable all new trades for the day."""
    risk = getattr(request.app.state, "risk_manager", None)
    if risk is None:
        return ORJSONResponse({"error": "risk manager not initialized"}, status_code=503)
    risk.force_disable("manual via dashboard API")
    return ORJSONResponse({"status": "disabled"})
