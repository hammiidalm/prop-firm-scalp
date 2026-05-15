"""Healthcheck / readiness endpoints.

* ``GET /health``  - Always 200 if the process is running (liveness).
* ``GET /ready``   - 200 only if the broker WS is connected and the risk
  manager is not disabled. Used by orchestrators to gate traffic or alert
  on degraded state.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import ORJSONResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def liveness() -> ORJSONResponse:
    """Liveness probe - returns 200 if the process is alive."""
    return ORJSONResponse({"status": "ok"})


@router.get("/ready")
async def readiness(request: Request) -> ORJSONResponse:
    """Readiness probe - checks broker WS connectivity and risk state."""
    checks: dict[str, bool] = {}

    ws = getattr(request.app.state, "ws_client", None)
    if ws is not None:
        checks["websocket_connected"] = ws.is_connected
        checks["ws_stale"] = ws.last_message_age < 60.0  # message within 60s
    else:
        checks["websocket_connected"] = False

    risk = getattr(request.app.state, "risk_manager", None)
    if risk is not None:
        snap = risk.snapshot()
        checks["risk_enabled"] = not snap.get("disabled", True)
    else:
        checks["risk_enabled"] = False

    all_ok = all(checks.values())
    status_code = 200 if all_ok else 503
    return ORJSONResponse(
        {"status": "ready" if all_ok else "degraded", "checks": checks},
        status_code=status_code,
    )
