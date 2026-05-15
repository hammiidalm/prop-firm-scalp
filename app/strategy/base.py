"""Strategy contract."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.models import Candle, Signal


@runtime_checkable
class Strategy(Protocol):
    """A strategy consumes candles and emits at most one Signal per bar."""

    name: str

    async def on_candle(self, candle: Candle) -> Signal | None: ...
