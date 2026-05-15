"""Account settings + risk parameter endpoints.

* ``GET  /api/v1/settings/account`` - Current account configuration
* ``PUT  /api/v1/settings/account`` - Switch environment (paper/demo/live)
* ``PUT  /api/v1/settings/risk``    - Update risk parameters at runtime
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel, Field

from app.utils.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["settings"])


# ---- Request models -------------------------------------------------------


class SwitchEnvironmentRequest(BaseModel):
    environment: str = Field(..., pattern=r"^(paper|demo|live)$")


class UpdateRiskRequest(BaseModel):
    max_daily_loss_pct: float = Field(..., ge=0.001, le=0.10)
    max_trades_per_day: int = Field(..., ge=1, le=50)


# ---- Endpoints ------------------------------------------------------------


@router.get("/settings/account")
async def get_account_settings(request: Request) -> ORJSONResponse:
    """Return the current account configuration."""
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return ORJSONResponse({"error": "engine not initialized"}, status_code=503)

    settings = engine._settings
    broker_type = "paper"
    if settings.app_mode.value in ("semi_auto", "full_auto"):
        broker_type = "tradelocker"

    return ORJSONResponse({
        "environment": settings.app_mode.value,
        "mode": settings.app_mode.value,
        "broker": broker_type,
        "account_id": settings.tl_account_id,
        "account_num": settings.tl_account_num,
        "server": settings.tl_server,
        "symbols": settings.symbols,
        "base_url": settings.tl_base_url,
    })


@router.put("/settings/account")
async def switch_environment(request: Request, body: SwitchEnvironmentRequest) -> ORJSONResponse:
    """Switch the trading environment (paper/demo/live) with hot-reload.

    This triggers a broker reconnection without restarting the container.
    The engine will:
    1. Close any open positions (gracefully)
    2. Disconnect the current broker + WS
    3. Update the in-memory mode
    4. Reconnect with the new broker configuration
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return ORJSONResponse({"error": "engine not initialized"}, status_code=503)

    target_env = body.environment
    current_mode = engine._settings.app_mode.value

    # Map 'demo' and 'live' to the corresponding TradingMode
    mode_map = {
        "paper": "paper",
        "demo": "semi_auto",   # demo uses TradeLocker demo server
        "live": "full_auto",   # live uses TradeLocker live server
    }
    target_mode = mode_map.get(target_env, target_env)

    if target_mode == current_mode:
        return ORJSONResponse({
            "status": "no_change",
            "message": f"Already running in '{target_env}' mode",
            "mode": current_mode,
            "reconnected": False,
        })

    log.info(
        "environment switch requested",
        extra={"from": current_mode, "to": target_env},
    )

    try:
        success = await engine.switch_environment(target_mode)
        if success:
            return ORJSONResponse({
                "status": "switched",
                "message": f"Switched to '{target_env}' successfully",
                "mode": engine._settings.app_mode.value,
                "reconnected": True,
            })
        else:
            return ORJSONResponse(
                {"error": "Switch failed - check logs", "mode": engine._settings.app_mode.value},
                status_code=500,
            )
    except Exception as exc:
        log.exception("environment switch error: %s", exc)
        return ORJSONResponse(
            {"error": f"Switch failed: {str(exc)}", "mode": engine._settings.app_mode.value},
            status_code=500,
        )


@router.put("/settings/risk")
async def update_risk_params(request: Request, body: UpdateRiskRequest) -> ORJSONResponse:
    """Update risk parameters at runtime without restart.

    Modifies:
    - max_daily_loss_pct: Daily drawdown circuit breaker threshold
    - max_trades_per_day: Hard daily trade limit
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return ORJSONResponse({"error": "engine not initialized"}, status_code=503)

    settings = engine._settings
    risk_manager = engine._risk

    old_dd = settings.max_daily_loss_pct
    old_trades = settings.max_trades_per_day

    # Update settings in-memory (mutable model fields)
    settings.max_daily_loss_pct = body.max_daily_loss_pct
    settings.max_trades_per_day = body.max_trades_per_day

    log.info(
        "risk parameters updated",
        extra={
            "daily_loss_pct": f"{old_dd} -> {body.max_daily_loss_pct}",
            "max_trades_per_day": f"{old_trades} -> {body.max_trades_per_day}",
        },
    )

    return ORJSONResponse({
        "status": "updated",
        "max_daily_loss_pct": body.max_daily_loss_pct,
        "max_trades_per_day": body.max_trades_per_day,
        "previous": {
            "max_daily_loss_pct": old_dd,
            "max_trades_per_day": old_trades,
        },
    })
