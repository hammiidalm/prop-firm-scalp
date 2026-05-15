"""OHLCV candle and a thin rolling-buffer container."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.utils.time import to_utc

if TYPE_CHECKING:
    import polars as pl


class Candle(BaseModel):
    """A single OHLCV bar. Immutable once created."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    timeframe: str
    timestamp: datetime  # bar open time, UTC
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(default=0.0, ge=0)

    @field_validator("timestamp")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        return to_utc(v)

    @field_validator("high")
    @classmethod
    def _high_consistent(cls, v: float, info: object) -> float:
        data = getattr(info, "data", {}) or {}
        o, low = data.get("open"), data.get("low")
        if o is not None and v < o:
            raise ValueError("high must be >= open")
        if low is not None and v < low:
            raise ValueError("high must be >= low")
        return v

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.close, self.open)

    @property
    def lower_wick(self) -> float:
        return min(self.close, self.open) - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open


class CandleSeries:
    """Bounded rolling buffer of candles for one (symbol, timeframe).

    Backed by ``collections.deque`` for O(1) append/popleft. Exposes a
    ``to_polars`` helper for analytics/backtesting.
    """

    __slots__ = ("symbol", "timeframe", "_buffer", "maxlen")

    def __init__(self, symbol: str, timeframe: str, maxlen: int = 1500) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.maxlen = maxlen
        self._buffer: deque[Candle] = deque(maxlen=maxlen)

    def __len__(self) -> int:
        return len(self._buffer)

    def __iter__(self) -> Iterator[Candle]:
        return iter(self._buffer)

    def __getitem__(self, idx: int) -> Candle:
        return self._buffer[idx]

    def append(self, candle: Candle) -> None:
        if candle.symbol != self.symbol or candle.timeframe != self.timeframe:
            raise ValueError(
                f"candle ({candle.symbol}/{candle.timeframe}) does not match "
                f"series ({self.symbol}/{self.timeframe})"
            )
        # Replace last bar if same timestamp (live tick updates), else append.
        if self._buffer and self._buffer[-1].timestamp == candle.timestamp:
            self._buffer[-1] = candle
        else:
            self._buffer.append(candle)

    def extend(self, candles: Iterable[Candle]) -> None:
        for c in candles:
            self.append(c)

    @property
    def last(self) -> Candle | None:
        return self._buffer[-1] if self._buffer else None

    def tail(self, n: int) -> list[Candle]:
        if n <= 0:
            return []
        if n >= len(self._buffer):
            return list(self._buffer)
        # deque slicing is O(n) but n is small (lookback windows)
        return list(self._buffer)[-n:]

    def to_polars(self) -> pl.DataFrame:
        import polars as pl

        rows = [c.model_dump() for c in self._buffer]
        return pl.DataFrame(rows) if rows else pl.DataFrame(
            schema={
                "symbol": pl.Utf8,
                "timeframe": pl.Utf8,
                "timestamp": pl.Datetime("us", "UTC"),
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Float64,
            }
        )
