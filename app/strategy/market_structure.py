"""Smart-money market-structure primitives: swing detection, BOS, CHOCH, sweeps.

Definitions used in this module
-------------------------------
* **Swing high (HH/LH)**: a candle whose high is greater than the highs of
  ``k`` neighbours on each side.
* **Swing low (HL/LL)**: symmetric definition for lows.
* **BOS (Break of Structure)**: price closes beyond the most recent swing
  in the direction of the prevailing trend - confirms continuation.
* **CHOCH (Change of Character)**: price closes beyond the most recent
  *opposing* swing - first signal of a possible trend reversal.
* **Liquidity sweep**: price *wicks* through a swing point but the bar
  closes back inside the prior range - typical stop-hunt that precedes a
  reversal.

The implementation is deliberately small, fully unit-testable, and produces
a stream of ``StructureEvent`` objects consumed by the scalp strategy.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from app.models import Candle


class SwingType(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"


class StructureKind(str, Enum):
    BOS_BULL = "BOS_BULL"
    BOS_BEAR = "BOS_BEAR"
    CHOCH_BULL = "CHOCH_BULL"
    CHOCH_BEAR = "CHOCH_BEAR"
    SWEEP_HIGH = "SWEEP_HIGH"   # liquidity grab above swing high (bearish bias)
    SWEEP_LOW = "SWEEP_LOW"     # liquidity grab below swing low (bullish bias)


@dataclass(frozen=True, slots=True)
class SwingPoint:
    type: SwingType
    price: float
    timestamp: datetime
    index: int


@dataclass(frozen=True, slots=True)
class StructureEvent:
    kind: StructureKind
    candle: Candle
    reference_swing: SwingPoint
    note: str = ""


@dataclass(slots=True)
class MarketStructure:
    """Stateful market-structure tracker fed one candle at a time.

    Parameters
    ----------
    swing_lookback:
        Number of bars on each side a candle must dominate to be marked as
        a swing high/low. ``2`` is a fast scalping value.
    sweep_wick_ratio:
        Minimum ratio (rejected wick / candle range) for a candle to count
        as a liquidity-sweep rejection. ``0.5`` requires the sweep wick to
        be at least half of the bar's total range.
    history:
        How many recent swing points to remember.
    """

    swing_lookback: int = 2
    sweep_wick_ratio: float = 0.5
    history: int = 50

    _candles: deque[Candle] = field(default_factory=lambda: deque(maxlen=512), init=False)
    _swings: deque[SwingPoint] = field(default_factory=lambda: deque(maxlen=50), init=False)
    _last_bos: StructureKind | None = field(default=None, init=False)

    # ---- public ---------------------------------------------------------
    def update(self, candle: Candle) -> list[StructureEvent]:
        """Ingest a new candle and return any structural events it triggered."""
        self._candles.append(candle)
        events: list[StructureEvent] = []

        # 1) Detect a *confirmed* swing on the candle that is `lookback` bars
        # old (it can no longer be invalidated by future bars within the
        # window).
        if len(self._candles) >= 2 * self.swing_lookback + 1:
            pivot_idx = len(self._candles) - self.swing_lookback - 1
            pivot = self._candles[pivot_idx]
            window = list(self._candles)[pivot_idx - self.swing_lookback : pivot_idx + self.swing_lookback + 1]
            if pivot.high == max(c.high for c in window):
                self._add_swing(SwingPoint(SwingType.HIGH, pivot.high, pivot.timestamp, pivot_idx))
            if pivot.low == min(c.low for c in window):
                self._add_swing(SwingPoint(SwingType.LOW, pivot.low, pivot.timestamp, pivot_idx))

        # 2) Liquidity sweeps - tested on the *current* candle against the
        # most recent unbroken swing in each direction.
        last_high = self._latest_swing(SwingType.HIGH)
        last_low = self._latest_swing(SwingType.LOW)

        if last_high is not None and self._is_sweep_high(candle, last_high.price):
            events.append(StructureEvent(
                kind=StructureKind.SWEEP_HIGH,
                candle=candle,
                reference_swing=last_high,
                note=f"wick above {last_high.price} closed back below",
            ))
        if last_low is not None and self._is_sweep_low(candle, last_low.price):
            events.append(StructureEvent(
                kind=StructureKind.SWEEP_LOW,
                candle=candle,
                reference_swing=last_low,
                note=f"wick below {last_low.price} closed back above",
            ))

        # 3) BOS / CHOCH - confirmed only when *close* breaks the swing.
        if last_high is not None and candle.close > last_high.price:
            kind = (
                StructureKind.CHOCH_BULL
                if self._last_bos is StructureKind.BOS_BEAR
                else StructureKind.BOS_BULL
            )
            events.append(StructureEvent(kind=kind, candle=candle, reference_swing=last_high))
            self._last_bos = StructureKind.BOS_BULL  # CHOCH flips trend
        elif last_low is not None and candle.close < last_low.price:
            kind = (
                StructureKind.CHOCH_BEAR
                if self._last_bos is StructureKind.BOS_BULL
                else StructureKind.BOS_BEAR
            )
            events.append(StructureEvent(kind=kind, candle=candle, reference_swing=last_low))
            self._last_bos = StructureKind.BOS_BEAR

        return events

    # ---- accessors ------------------------------------------------------
    def latest_swing_high(self) -> SwingPoint | None:
        return self._latest_swing(SwingType.HIGH)

    def latest_swing_low(self) -> SwingPoint | None:
        return self._latest_swing(SwingType.LOW)

    def last_trend(self) -> StructureKind | None:
        return self._last_bos

    # ---- internals ------------------------------------------------------
    def _add_swing(self, swing: SwingPoint) -> None:
        # Avoid duplicate registration if the same swing is detected twice
        # (e.g. equal highs).
        if self._swings and self._swings[-1].timestamp == swing.timestamp and self._swings[-1].type == swing.type:
            return
        self._swings.append(swing)

    def _latest_swing(self, kind: SwingType) -> SwingPoint | None:
        for s in reversed(self._swings):
            if s.type == kind:
                return s
        return None

    def _is_sweep_high(self, candle: Candle, swing_high: float) -> bool:
        if candle.high <= swing_high:
            return False
        if candle.close >= swing_high:
            return False  # closed above -> BOS, not a sweep
        if candle.range == 0:
            return False
        rejection_wick = candle.high - max(candle.close, candle.open)
        return rejection_wick / candle.range >= self.sweep_wick_ratio

    def _is_sweep_low(self, candle: Candle, swing_low: float) -> bool:
        if candle.low >= swing_low:
            return False
        if candle.close <= swing_low:
            return False  # closed below -> BOS, not a sweep
        if candle.range == 0:
            return False
        rejection_wick = min(candle.close, candle.open) - candle.low
        return rejection_wick / candle.range >= self.sweep_wick_ratio
