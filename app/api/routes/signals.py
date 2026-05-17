"""Signal API route — exposes the latest strategy signal."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import ORJSONResponse

from app.utils.logging import get_logger

log = get_logger(__name__)

router = APIRouter()


@router.get("/signals/latest")
async def latest_signal(request: Request) -> ORJSONResponse:
    """Return the most recent signal from the strategy engine."""
    engine = request.app.state.engine
    signal = engine.last_signal if engine else None

    if signal is None:
        return ORJSONResponse({"signal": None})

    return ORJSONResponse({
        "signal": {
            "symbol": signal.symbol,
            "direction": "LONG" if signal.direction.name == "LONG" else "SHORT",
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "confidence": signal.confidence if hasattr(signal, "confidence") else 0.5,
            "reason": signal.reason,
            "session": signal.session,
            "timeframe": signal.timeframe,
            "rr_ratio": round(signal.rr_ratio, 2),
            "generated_at": signal.generated_at.isoformat() if hasattr(signal, "generated_at") else "",
            "structure_tags": signal.structure_tags,
        }
    })
