"""REST price poller — fallback when WebSocket is unavailable."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import get_settings
from app.utils.logging import get_logger

log = get_logger(__name__)

POLL_INTERVAL_SEC = 5  # poll every 5s to build candles

# Reverse mapping seconds -> timeframe string for candle metadata
_SECONDS_TO_TF: dict[int, str] = {
    60: "M1", 300: "M5", 900: "M15", 1800: "M30",
    3600: "H1", 14400: "H4", 86400: "D1",
}


@dataclass
class _CandleBuffer:
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    ts_start: float  # monotonic time of candle start
    last_ask: float = 0.0
    last_bid: float = 0.0


CandleHandler = Callable[[dict[str, Any]], Awaitable[None]]


class RestPricePoller:
    """Poll TradeLocker REST /trade/quotes and produce synthetic candles."""

    def __init__(
        self,
        symbols: list[str] | None = None,
        on_candle: CandleHandler | None = None,
        candle_seconds: int | dict[str, int] = 60,
    ) -> None:
        s = get_settings()
        self._base_url = s.tl_base_url.rstrip("/")
        self._email = s.tl_email.get_secret_value()
        self._password = s.tl_password.get_secret_value()
        self._server = s.tl_server
        self._acc_num = s.tl_account_num or "3"
        self._symbols = symbols or s.symbols
        self._on_candle = on_candle
        # Normalize candle_seconds to dict for per-symbol lookups
        if isinstance(candle_seconds, int):
            self._candle_seconds: dict[str, int] = {
                sym: candle_seconds for sym in self._symbols
            }
        else:
            self._candle_seconds = candle_seconds
        self._http: httpx.AsyncClient | None = None
        self._token: str = ""
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

        # Instrument lookup: symbol -> {tradableInstrumentId, routeId}
        self._instruments: dict[str, dict[str, int]] = {}
        self._buffers: dict[str, _CandleBuffer] = {}
        self._last_quotes: dict[str, dict[str, float]] = {}

    async def start(self) -> None:
        self._stop.clear()
        self._http = httpx.AsyncClient(base_url=self._base_url, timeout=10)
        await self._refresh_token()
        await self._resolve_instruments()
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
        if self._http:
            await self._http.aclose()

    async def _refresh_token(self) -> None:
        assert self._http is not None
        resp = await self._http.post(
            "/auth/jwt/token",
            json={
                "email": self._email,
                "password": self._password,
                "server": self._server,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["accessToken"]
        log.info("rest_poller: jwt refreshed")

    async def _resolve_instruments(self) -> None:
        """Fetch instrument IDs for each symbol."""
        assert self._http is not None
        # Use the token from _refresh_token
        acc_id = get_settings().tl_account_id
        resp = await self._http.get(
            f"/trade/accounts/{acc_id}/instruments",
            headers={
                "Authorization": f"Bearer {self._token}",
                "accNum": self._acc_num,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("s") != "ok":
            log.error("rest_poller: cannot resolve instruments: %s", data.get("errmsg"))
            return

        instruments = data.get("d", {}).get("instruments", [])
        for inst in instruments:
            name = inst.get("name", "")
            if name not in self._symbols:
                continue
            # Use INFO route for quotes
            info_route = next(
                (r["id"] for r in inst.get("routes", []) if r["type"] == "INFO"),
                None,
            )
            trade_id = inst.get("tradableInstrumentId")
            if info_route and trade_id:
                self._instruments[name] = {
                    "tradableInstrumentId": trade_id,
                    "routeId": info_route,
                }
                log.info(
                    "rest_poller: resolved %s", name,
                    extra={"id": trade_id, "route": info_route},
                )

    async def _poll(self) -> dict[str, dict[str, float]] | None:
        """Fetch latest quotes for all known instruments. Returns {symbol: {bid, ask, mid}}."""
        assert self._http is not None
        prices: dict[str, dict[str, float]] = {}

        for sym, info in self._instruments.items():
            try:
                resp = await self._http.get(
                    "/trade/quotes",
                    params={
                        "routeId": info["routeId"],
                        "tradableInstrumentId": info["tradableInstrumentId"],
                    },
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "accNum": self._acc_num,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("s") == "ok":
                    d = data["d"]
                    ask = float(d.get("ap", 0))
                    bid = float(d.get("bp", 0))
                    prices[sym] = {
                        "bid": bid,
                        "ask": ask,
                        "mid": (bid + ask) / 2 if bid and ask else 0,
                    }
                else:
                    log.warning("rest_poller: quote failed %s: %s", sym, data.get("errmsg"))
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    await self._refresh_token()
                else:
                    log.warning("rest_poller: http error %s: %d", sym, e.response.status_code)
            except Exception as e:
                log.warning("rest_poller: quote error %s: %s", sym, e)

        return prices or None

    def _produce_candle(self, sym: str, price: float) -> dict[str, Any] | None:
        """Accumulate price into buffer. Returns completed candle when 1min elapses."""
        now = time.monotonic()
        buf = self._buffers.get(sym)

        if buf is None:
            self._buffers[sym] = _CandleBuffer(
                symbol=sym, open=price, high=price, low=price, close=price,
                volume=0.0, ts_start=now, last_ask=price, last_bid=price,
            )
            return None

        # Update OHLC within this candle
        buf.high = max(buf.high, price)
        buf.low = min(buf.low, price)
        buf.close = price
        buf.volume += 1.0

        # Check if configured candle duration elapsed
        candle_secs = self._candle_seconds.get(sym, 60)
        if now - buf.ts_start >= candle_secs:
            ts = datetime.now(timezone.utc)
            candle = {
                "symbol": sym,
                "timeframe": _SECONDS_TO_TF.get(candle_secs, "M1"),
                "timestamp": ts,
                "open": buf.open,
                "high": buf.high,
                "low": buf.low,
                "close": buf.close,
                "volume": buf.volume,
            }
            # Start next candle
            self._buffers[sym] = _CandleBuffer(
                symbol=sym, open=price, high=price, low=price, close=price,
                volume=0.0, ts_start=now, last_ask=price, last_bid=price,
            )
            return candle

        return None

    async def _poll_loop(self) -> None:
        log.info(
            "rest_poller: started",
            extra={"symbols": list(self._instruments.keys()), "interval": POLL_INTERVAL_SEC},
        )
        tick = 0
        while not self._stop.is_set():
            try:
                prices = await self._poll()
                if prices:
                    self._last_quotes = prices
                    tick += 1
                    log.info("rest_poller: tick %d quotes=%s", tick, {k: v.get("mid") for k, v in prices.items()})
                    for sym, p in prices.items():
                        if p["mid"] > 0:
                            candle = self._produce_candle(sym, p["mid"])
                            if candle and self._on_candle:
                                log.info("rest_poller: candle %s o=%.5f h=%.5f l=%.5f c=%.5f",
                                         sym, candle["open"], candle["high"], candle["low"], candle["close"])
                                await self._on_candle(candle)
                else:
                    log.warning("rest_poller: no prices returned")
            except Exception as e:
                log.error("rest_poller: loop error: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_SEC)
            except TimeoutError:
                continue

    @property
    def is_connected(self) -> bool:
        return bool(self._instruments) and bool(self._token)

    @property
    def latest_prices(self) -> dict[str, dict[str, float]]:
        """Return the most recently polled prices per symbol."""
        return dict(self._last_quotes) if hasattr(self, '_last_quotes') else {}
