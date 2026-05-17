"""Market data endpoints — current prices from REST poller or Finnhub."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import ORJSONResponse

router = APIRouter(tags=["market"])


@router.get("/market/prices")
async def get_prices(request: Request) -> ORJSONResponse:
    """Return latest known prices for all instruments.

    Source: Finnhub (if active) or TradeLocker REST poller.
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return ORJSONResponse({"prices": {}})

    poller = getattr(engine, "_rest_poller", None)
    if poller is None:
        return ORJSONResponse({"prices": {}})

    return ORJSONResponse({"prices": poller.latest_prices})


@router.get("/market/price/{symbol}")
async def get_price(symbol: str, request: Request) -> ORJSONResponse:
    """Return the latest price for a specific symbol.

    Tries the poller cache first; if empty, falls back to Finnhub direct query.
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return ORJSONResponse({"symbol": symbol, "price": 0.0, "source": "unavailable"})

    poller = getattr(engine, "_rest_poller", None)
    if poller and hasattr(poller, "latest_prices"):
        price = poller.latest_prices.get(symbol, 0.0)
        if price > 0:
            source = "finnhub" if hasattr(poller, "_client") else "tradelocker"
            return ORJSONResponse({"symbol": symbol, "price": price, "source": source})

    # Direct Finnhub fallback
    from app.config import get_settings
    settings = get_settings()
    if settings.finnhub_api_key.get_secret_value():
        from app.integrations.finnhub.client import FinnhubClient
        client = FinnhubClient(settings.finnhub_api_key.get_secret_value())
        try:
            price = await client.get_price(symbol)
            return ORJSONResponse({"symbol": symbol, "price": price, "source": "finnhub"})
        finally:
            await client.close()

    return ORJSONResponse({"symbol": symbol, "price": 0.0, "source": "unavailable"})