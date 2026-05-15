"""Comprehensive bot status + performance endpoints.

* ``GET /api/v1/status``      - Full engine runtime state
* ``GET /api/v1/performance`` - Performance metrics (winrate, PF, PnL, DD)
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import ORJSONResponse

router = APIRouter(tags=["status"])


@router.get("/status")
async def get_status(request: Request) -> ORJSONResponse:
    """Return comprehensive bot status for the Telegram /status command."""
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return ORJSONResponse({"error": "engine not initialized"}, status_code=503)

    settings = engine._settings
    risk = engine._risk
    ws = engine._ws
    executor = engine._executor

    # WebSocket state
    ws_connected = False
    ws_reconnects = 0
    if ws is not None:
        ws_connected = ws.is_connected
        ws_reconnects = ws.reconnect_count

    # Risk state
    balance = 0.0
    daily_dd_pct = 0.0
    trades_today = 0
    risk_disabled = True
    if risk is not None:
        snap = risk.snapshot()
        balance = snap.get("balance", 0.0)
        trades_today = snap.get("trades_today", 0)
        daily_dd_pct = snap.get("drawdown_pct", 0.0)
        risk_disabled = snap.get("disabled", True)

    # Open positions count
    open_count = 0
    if executor is not None:
        open_count = len(executor.open_trades())

    return ORJSONResponse({
        "mode": settings.app_mode.value,
        "environment": settings.app_mode.value,
        "ws_connected": ws_connected,
        "ws_reconnects": ws_reconnects,
        "balance": balance,
        "daily_drawdown_pct": daily_dd_pct,
        "trades_today": trades_today,
        "open_positions": open_count,
        "risk_disabled": risk_disabled,
        "symbols": settings.symbols,
        "max_trades_per_day": settings.max_trades_per_day,
        "max_daily_loss_pct": settings.max_daily_loss_pct,
    })


@router.get("/performance")
async def get_performance(request: Request) -> ORJSONResponse:
    """Return performance summary for the Telegram /performance command."""
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return ORJSONResponse({"error": "engine not initialized"}, status_code=503)

    stats = engine._stats
    risk = engine._risk

    if stats is None:
        return ORJSONResponse({
            "total_trades": 0,
            "winrate": 0.0,
            "profit_factor": 0.0,
            "total_pnl": 0.0,
            "max_drawdown_pct": 0.0,
            "avg_rr": 0.0,
        })

    trades = stats.trades
    total_trades = len(trades)
    wins = sum(1 for t in trades if (t.pnl or 0) > 0)
    losses = sum(1 for t in trades if (t.pnl or 0) < 0)
    winrate = wins / total_trades if total_trades else 0.0

    # Profit factor = gross profit / gross loss
    gross_profit = sum((t.pnl or 0) for t in trades if (t.pnl or 0) > 0)
    gross_loss = abs(sum((t.pnl or 0) for t in trades if (t.pnl or 0) < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0

    total_pnl = stats.total_pnl
    max_dd = stats.max_drawdown

    # Average R:R from trades that have rr_ratio set
    rr_values = [t.rr_ratio for t in trades if t.rr_ratio is not None and t.rr_ratio > 0]
    avg_rr = sum(rr_values) / len(rr_values) if rr_values else 0.0

    return ORJSONResponse({
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "winrate": winrate,
        "profit_factor": profit_factor,
        "total_pnl": total_pnl,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "max_drawdown_pct": max_dd,
        "avg_rr": avg_rr,
        "equity_curve_length": len(stats.equity_curve),
    })
