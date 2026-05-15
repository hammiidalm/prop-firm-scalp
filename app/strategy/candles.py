"""Single-candle pattern primitives used by the scalp strategy."""

from __future__ import annotations

from app.models import Candle


def is_strong_bullish_rejection(c: Candle, min_wick_ratio: float = 0.55) -> bool:
    """Long lower wick + bullish close - classic hammer / pin-bar long."""
    if c.range == 0:
        return False
    return (
        c.is_bullish
        and (c.lower_wick / c.range) >= min_wick_ratio
        and c.body / c.range <= 0.5
    )


def is_strong_bearish_rejection(c: Candle, min_wick_ratio: float = 0.55) -> bool:
    if c.range == 0:
        return False
    return (
        c.is_bearish
        and (c.upper_wick / c.range) >= min_wick_ratio
        and c.body / c.range <= 0.5
    )


def is_displacement(c: Candle, atr: float, multiplier: float = 1.5) -> bool:
    """A 'displacement' candle is a high-momentum bar > ``multiplier * ATR``."""
    if atr <= 0:
        return False
    return c.range >= multiplier * atr and (c.body / max(c.range, 1e-9)) >= 0.65


def atr(candles: list[Candle], period: int = 14) -> float:
    """Simple ATR over the last ``period`` bars (no Wilder smoothing)."""
    if len(candles) < 2:
        return 0.0
    take = candles[-(period + 1):]
    trs: list[float] = []
    for prev, cur in zip(take, take[1:], strict=False):
        tr = max(
            cur.high - cur.low,
            abs(cur.high - prev.close),
            abs(cur.low - prev.close),
        )
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0
