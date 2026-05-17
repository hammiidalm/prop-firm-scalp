"""Market data endpoints — prices from TradeLocker REST poller."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel

router = APIRouter(tags=["market"])


# ── single-symbol price ───────────────────────────────────────────────────────

class PriceResponse(BaseModel):
    symbol: str
    price: float | None = None
    source: str | None = None  # "poller" | "unavailable"


@router.get("/market/price/{symbol}", response_model=PriceResponse)
async def get_price(symbol: str, request: Request) -> PriceResponse:
    """Return the latest price for *symbol* from the engine poller cache."""
    engine = getattr(request.app.state, "engine", None)
    if engine:
        poller = getattr(engine, "_poller", None)
        if poller:
            quotes = poller.latest_prices
            sym_key = symbol.upper()
            q = quotes.get(sym_key)
            if q:
                return PriceResponse(
                    symbol=sym_key,
                    price=q.get("mid"),
                    source="poller",
                )

    return PriceResponse(symbol=symbol.upper(), price=None, source="unavailable")


# ── all known prices ────────────────────────────────────────────────────────

@router.get("/market/prices")
async def get_prices(request: Request) -> ORJSONResponse:
    """Return latest known prices for all instruments from the poller."""
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return ORJSONResponse({"prices": {}})

    poller = getattr(engine, "_poller", None)
    if poller is None:
        return ORJSONResponse({"prices": {}})

    return ORJSONResponse({"prices": poller.latest_prices})
