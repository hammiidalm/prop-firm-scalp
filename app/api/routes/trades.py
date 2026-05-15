"""Trade journal + open positions endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import ORJSONResponse

router = APIRouter(tags=["trades"])


@router.get("/trades")
async def list_trades(request: Request, limit: int = Query(default=50, le=500)) -> ORJSONResponse:
    """Return recent trade journal entries (most recent first)."""
    journal = getattr(request.app.state, "journal", None)
    if journal is None:
        return ORJSONResponse({"trades": []})
    rows = await journal.list_recent(limit=limit)
    # Convert ORM rows to dicts
    trades = []
    for row in rows:
        trades.append({
            "trade_id": row.trade_id,
            "symbol": row.symbol,
            "direction": row.direction,
            "entry_time": str(row.entry_time),
            "entry_price": row.entry_price,
            "stop_loss": row.stop_loss,
            "take_profit": row.take_profit,
            "quantity": row.quantity,
            "exit_time": str(row.exit_time) if row.exit_time else None,
            "exit_price": row.exit_price,
            "pnl": row.pnl,
            "pnl_pct": row.pnl_pct,
            "status": row.status,
            "session": row.session,
            "entry_reason": row.entry_reason,
            "exit_reason": row.exit_reason,
            "spread_pips": row.spread_pips,
            "slippage_pips": row.slippage_pips,
            "latency_ms": row.latency_ms,
            "rr_ratio": row.rr_ratio,
        })
    return ORJSONResponse({"trades": trades, "count": len(trades)})


@router.get("/trades/open")
async def open_positions(request: Request) -> ORJSONResponse:
    """Return currently open positions tracked by the executor."""
    executor = getattr(request.app.state, "executor", None)
    if executor is None:
        return ORJSONResponse({"positions": []})
    open_trades = executor.open_trades()
    positions = [t.model_dump(mode="json") for t in open_trades.values()]
    return ORJSONResponse({"positions": positions, "count": len(positions)})
