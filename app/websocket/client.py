"""Resilient async WebSocket client for TradeLocker.

Features
--------
* Auto-reconnect with exponential backoff + jitter.
* Heartbeat / ping monitoring.
* JSON message decoding via ``orjson``.
* Pluggable ``on_message`` callback so the engine receives every event.
* Latency timestamping (server send -> local receive) on every message.

The TradeLocker WS protocol uses JSON envelopes with a ``type`` field for
``quote``, ``order``, ``position`` etc. We don't hard-code the schema here -
the engine layer is responsible for interpreting the payload.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import orjson
import websockets
from websockets.client import WebSocketClientProtocol
from websockets.exceptions import ConnectionClosed

from app.utils.logging import get_logger

log = get_logger(__name__)

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class WebSocketClient:
    def __init__(
        self,
        url: str,
        *,
        token_provider: Callable[[], Awaitable[str]],
        on_message: MessageHandler,
        ping_interval: float = 20.0,
        ping_timeout: float = 10.0,
        max_backoff: float = 30.0,
    ) -> None:
        self._url = url
        self._token_provider = token_provider
        self._on_message = on_message
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._max_backoff = max_backoff
        self._ws: WebSocketClientProtocol | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_message_at: float = 0.0
        self._reconnects = 0

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    @property
    def reconnect_count(self) -> int:
        return self._reconnects

    @property
    def last_message_age(self) -> float:
        if self._last_message_at == 0.0:
            return float("inf")
        return time.monotonic() - self._last_message_at

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="ws-client")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def send(self, payload: dict[str, Any]) -> None:
        if not self._ws or self._ws.closed:
            raise RuntimeError("websocket not connected")
        await self._ws.send(orjson.dumps(payload).decode())

    # ---- internal -------------------------------------------------------
    async def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                token = await self._token_provider()
                headers = [("Authorization", f"Bearer {token}")]
                async with websockets.connect(
                    self._url,
                    additional_headers=headers,
                    ping_interval=self._ping_interval,
                    ping_timeout=self._ping_timeout,
                    close_timeout=5.0,
                    max_size=8 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    self._reconnects = attempt
                    log.info(
                        "websocket connected",
                        extra={"url": self._url, "attempt": attempt},
                    )
                    attempt = 0
                    await self._consume(ws)
            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, OSError) as exc:
                log.warning("ws disconnected: %s", exc)
            except Exception as exc:  # noqa: BLE001
                log.exception("ws unexpected error: %s", exc)
            finally:
                self._ws = None

            if self._stop.is_set():
                break

            attempt += 1
            backoff = min(self._max_backoff, (2 ** min(attempt, 6)) + random.random())
            log.info("ws reconnect in %.1fs (attempt=%d)", backoff, attempt)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                break  # stop requested during backoff
            except TimeoutError:
                continue

    async def _consume(self, ws: WebSocketClientProtocol) -> None:
        async for raw in ws:
            self._last_message_at = time.monotonic()
            try:
                msg = orjson.loads(raw if isinstance(raw, (bytes, bytearray)) else raw.encode())
            except orjson.JSONDecodeError:
                log.warning("ws non-json payload dropped")
                continue
            try:
                await self._on_message(msg)
            except Exception as exc:  # noqa: BLE001
                # Never let a handler exception kill the consumer loop.
                log.exception("ws handler error: %s", exc)
