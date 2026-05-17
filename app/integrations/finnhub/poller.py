"""Finnhub-based price poller — drop-in replacement for RestPricePoller.

When FINNHUB_API_KEY is set, the engine uses this instead of TradeLocker REST
for price data. Same interface: calls `on_candle(candle_dict)` per completed bar.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from app.config import get_settings
from app.integrations.finnhub.client import FinnhubClient
from app.utils.logging import get_logger

log = get_logger(__name__)

CandleHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class FinnhubPoller:
    """Poll Finnhub for candle data at configured intervals.

    Produces the same candle_dict format as RestPricePoller so the engine
    can use it without modification.
    """

    def __init__(
        self,
        symbols: list[str],
        on_candle: CandleHandler,
        candle_seconds: dict[str, int] | int = 60,
    ) -> None:
        settings = get_settings()
        self._client = FinnhubClient(api_key=settings.finnhub_api_key.get_secret_value())
        self._symbols = symbols
        self._on_candle = on_candle

        if isinstance(candle_seconds, int):
            self._candle_seconds = {sym: candle_seconds for sym in symbols}
        else:
            self._candle_seconds = candle_seconds

        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._last_ts: dict[str, int] = {}  # last candle timestamp per symbol

    @property
    def latest_prices(self) -> dict[str, float]:
        """Latest prices (for /market/prices endpoint compatibility)."""
        return self._prices.copy()

    def __init_subclass__(cls) -> None:
        pass

    async def start(self) -> None:
        """Start the polling loop."""
        self._stop.clear()
        self._prices: dict[str, float] = {}
        self._task = asyncio.create_task(self._poll_loop(), name="finnhub-poller")
        log.info("finnhub_poller_started", extra={"symbols": self._symbols})

    async def stop(self) -> None:
        """Stop the polling loop."""
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._client.close()
        log.info("finnhub_poller_stopped")

    async def _poll_loop(self) -> None:
        """Main poll loop — fetch candles for each symbol at their interval."""
        while not self._stop.is_set():
            for symbol in self._symbols:
                try:
                    await self._fetch_and_emit(symbol)
                except Exception as e:
                    log.error("finnhub_poll_error", extra={"symbol": symbol, "error": str(e)})

            # Sleep for the minimum candle interval (avoid over-polling)
            min_interval = min(self._candle_seconds.values(), default=60)
            # Finnhub rate limit: 60/min. Sleep enough to stay safe.
            sleep_time = max(min_interval, 5)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_time)
                break  # stop event was set
            except asyncio.TimeoutError:
                continue  # normal timeout, keep polling

    async def _fetch_and_emit(self, symbol: str) -> None:
        """Fetch latest candle(s) for a symbol and emit via on_candle callback."""
        candle_seconds = self._candle_seconds.get(symbol, 60)

        # Determine resolution from candle_seconds
        resolution = self._seconds_to_resolution(candle_seconds)

        # Fetch last 2 bars (current may be incomplete, we want the last closed one)
        now_ts = int(time.time())
        from_ts = now_ts - (candle_seconds * 3)  # fetch 3 bars worth

        candles = await self._client.get_candles(
            symbol=symbol,
            resolution=resolution,
            from_ts=from_ts,
            to_ts=now_ts,
        )

        if not candles:
            return

        # Update latest price
        self._prices[symbol] = candles[-1].close

        # Emit only candles we haven't seen yet
        last_seen = self._last_ts.get(symbol, 0)
        for candle in candles:
            candle_ts = int(candle.timestamp.timestamp())
            if candle_ts > last_seen:
                self._last_ts[symbol] = candle_ts
                # Emit in the same dict format as RestPricePoller
                await self._on_candle({
                    "symbol": candle.symbol,
                    "timeframe": candle.timeframe,
                    "timestamp": candle.timestamp,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                })

    @staticmethod
    def _seconds_to_resolution(seconds: int) -> str:
        """Convert candle interval in seconds to TF label."""
        mapping = {
            60: "M1",
            300: "M5",
            900: "M15",
            1800: "M30",
            3600: "M60",
            14400: "H4",
            86400: "D1",
        }
        return mapping.get(seconds, "M1")
