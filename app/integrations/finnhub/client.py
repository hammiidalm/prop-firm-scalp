"""Finnhub.io REST client for forex/crypto candles and quotes.

Provides candle history and live price as an alternative data source
to TradeLocker REST/WS. Used when FINNHUB_API_KEY is set.

Endpoints used:
- GET /api/v1/forex/candle — historical OHLCV
- GET /api/v1/quote — last price (stocks/crypto)
- GET /api/v1/forex/symbol — symbol list (for validation)

Rate limit: 60 calls/minute on free tier.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.models.candle import Candle
from app.utils.logging import get_logger

log = get_logger(__name__)

_BASE_URL = "https://finnhub.io/api/v1"

# Mapping: internal TF label → Finnhub resolution string
_RESOLUTION_MAP: dict[str, str] = {
    "M1": "1",
    "M5": "5",
    "M15": "15",
    "M30": "30",
    "M60": "60",
    "H1": "60",
    "H4": "240",
    "D1": "D",
    "D": "D",
    "W": "W",
}

# Mapping: internal symbol → Finnhub forex symbol format
_SYMBOL_MAP: dict[str, str] = {
    "EURUSD": "OANDA:EUR_USD",
    "GBPUSD": "OANDA:GBP_USD",
    "USDJPY": "OANDA:USD_JPY",
    "USDCHF": "OANDA:USD_CHF",
    "AUDUSD": "OANDA:AUD_USD",
    "NZDUSD": "OANDA:NZD_USD",
    "USDCAD": "OANDA:USD_CAD",
    "EURGBP": "OANDA:EUR_GBP",
    "EURJPY": "OANDA:EUR_JPY",
    "GBPJPY": "OANDA:GBP_JPY",
    "XAUUSD": "OANDA:XAU_USD",
    "XAGUSD": "OANDA:XAG_USD",
    "BTCUSD": "OANDA:BTC_USD",
    "ETHUSD": "OANDA:ETH_USD",
}


def _to_finnhub_symbol(symbol: str) -> str:
    """Convert internal symbol to Finnhub format."""
    mapped = _SYMBOL_MAP.get(symbol.upper())
    if mapped:
        return mapped
    # Attempt auto-conversion: EURUSD → OANDA:EUR_USD
    sym = symbol.upper()
    if len(sym) == 6:
        return f"OANDA:{sym[:3]}_{sym[3:]}"
    return sym


class FinnhubClient:
    """Async client for Finnhub.io REST API.

    Usage:
        client = FinnhubClient(api_key="your_key")
        candles = await client.get_candles("EURUSD", "M5", from_ts, to_ts)
        price = await client.get_price("EURUSD")
        await client.close()
    """

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("FINNHUB_API_KEY is required but empty")
        self._api_key = api_key
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy-init HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=_BASE_URL,
                params={"token": self._api_key},
                timeout=httpx.Timeout(15.0),
            )
        return self._client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_candles(
        self,
        symbol: str,
        resolution: str,
        from_ts: int,
        to_ts: int,
    ) -> list[Candle]:
        """Fetch historical candles from Finnhub forex/candle endpoint.

        Args:
            symbol: Internal symbol (e.g. "EURUSD"). Auto-mapped to Finnhub format.
            resolution: Timeframe label (M1, M5, M15, etc). Mapped to Finnhub resolution.
            from_ts: Start Unix timestamp (seconds).
            to_ts: End Unix timestamp (seconds).

        Returns:
            List of Candle objects sorted by timestamp ascending.
        """
        finnhub_symbol = _to_finnhub_symbol(symbol)
        finnhub_resolution = _RESOLUTION_MAP.get(resolution.upper(), "1")

        client = await self._get_client()

        log.info(
            "finnhub_get_candles",
            extra={
                "symbol": finnhub_symbol,
                "resolution": finnhub_resolution,
                "from": from_ts,
                "to": to_ts,
            },
        )

        try:
            resp = await client.get(
                "/forex/candle",
                params={
                    "symbol": finnhub_symbol,
                    "resolution": finnhub_resolution,
                    "from": from_ts,
                    "to": to_ts,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            log.error("finnhub_candle_http_error", extra={"status": e.response.status_code})
            return []
        except httpx.RequestError as e:
            log.error("finnhub_candle_connection_error", extra={"error": str(e)})
            return []

        # Finnhub returns: {"s": "ok", "t": [...], "o": [...], "h": [...], "l": [...], "c": [...], "v": [...]}
        if data.get("s") != "ok":
            log.warning("finnhub_candle_no_data", extra={"status": data.get("s"), "symbol": symbol})
            return []

        timestamps = data.get("t", [])
        opens = data.get("o", [])
        highs = data.get("h", [])
        lows = data.get("l", [])
        closes = data.get("c", [])
        volumes = data.get("v", [])

        candles: list[Candle] = []
        for i in range(len(timestamps)):
            candles.append(Candle(
                symbol=symbol,
                timeframe=resolution,
                timestamp=datetime.fromtimestamp(timestamps[i], tz=timezone.utc),
                open=float(opens[i]),
                high=float(highs[i]),
                low=float(lows[i]),
                close=float(closes[i]),
                volume=float(volumes[i]) if i < len(volumes) else 0.0,
            ))

        log.info("finnhub_candles_received", extra={"symbol": symbol, "count": len(candles)})
        return candles

    async def get_price(self, symbol: str) -> float:
        """Get the last price for a symbol from Finnhub quote endpoint.

        Args:
            symbol: Internal symbol (e.g. "EURUSD").

        Returns:
            Last known price (current price). Returns 0.0 on error.
        """
        finnhub_symbol = _to_finnhub_symbol(symbol)
        client = await self._get_client()

        try:
            resp = await client.get("/quote", params={"symbol": finnhub_symbol})
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            log.error("finnhub_quote_error", extra={"symbol": symbol, "error": str(e)})
            return 0.0

        # Finnhub quote: {"c": current, "h": high, "l": low, "o": open, "pc": previous_close}
        price = data.get("c", 0.0)
        if not price:
            log.warning("finnhub_quote_empty", extra={"symbol": symbol, "data": data})
            return 0.0

        return float(price)
