"""Tests for the MarketStructure swing/BOS/CHOCH/sweep detection."""

from datetime import UTC, datetime, timedelta

import pytest

from app.models import Candle
from app.strategy.market_structure import MarketStructure, StructureKind, SwingType


def _candle(idx: int, open_: float, high: float, low: float, close: float) -> Candle:
    ts = datetime(2024, 6, 10, 8, 0, tzinfo=UTC) + timedelta(minutes=idx)
    return Candle(
        symbol="EURUSD",
        timeframe="M1",
        timestamp=ts,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100.0,
    )


class TestSwingDetection:
    def test_detects_swing_high(self):
        """A candle with the highest high in its lookback window is a swing high."""
        ms = MarketStructure(swing_lookback=2)
        # 5 candles: low, low, HIGH, low, low
        candles = [
            _candle(0, 1.0850, 1.0855, 1.0845, 1.0852),
            _candle(1, 1.0852, 1.0858, 1.0848, 1.0855),
            _candle(2, 1.0855, 1.0880, 1.0850, 1.0870),  # swing high
            _candle(3, 1.0870, 1.0875, 1.0860, 1.0862),
            _candle(4, 1.0862, 1.0868, 1.0855, 1.0858),
        ]
        all_events = []
        for c in candles:
            all_events.extend(ms.update(c))
        # After 5 candles, swing at index 2 should be detected
        assert ms.latest_swing_high() is not None
        assert ms.latest_swing_high().price == pytest.approx(1.0880)

    def test_detects_swing_low(self):
        ms = MarketStructure(swing_lookback=2)
        candles = [
            _candle(0, 1.0860, 1.0865, 1.0855, 1.0858),
            _candle(1, 1.0858, 1.0862, 1.0850, 1.0852),
            _candle(2, 1.0852, 1.0856, 1.0830, 1.0835),  # swing low
            _candle(3, 1.0835, 1.0850, 1.0832, 1.0848),
            _candle(4, 1.0848, 1.0860, 1.0845, 1.0857),
        ]
        for c in candles:
            ms.update(c)
        assert ms.latest_swing_low() is not None
        assert ms.latest_swing_low().price == pytest.approx(1.0830)


class TestLiquiditySweep:
    def test_sweep_low_detected(self):
        """A candle that wicks below a swing low but closes above is a sweep."""
        ms = MarketStructure(swing_lookback=2, sweep_wick_ratio=0.4)
        # Build a swing low at 1.0830
        setup = [
            _candle(0, 1.0860, 1.0865, 1.0855, 1.0858),
            _candle(1, 1.0858, 1.0862, 1.0840, 1.0842),
            _candle(2, 1.0842, 1.0848, 1.0830, 1.0845),  # swing low @ 1.0830
            _candle(3, 1.0845, 1.0860, 1.0843, 1.0858),
            _candle(4, 1.0858, 1.0865, 1.0850, 1.0862),
        ]
        for c in setup:
            ms.update(c)

        # Now a sweep candle: wicks to 1.0825 but closes at 1.0850
        sweep = _candle(5, 1.0850, 1.0855, 1.0825, 1.0850)
        events = ms.update(sweep)
        sweep_events = [e for e in events if e.kind is StructureKind.SWEEP_LOW]
        assert len(sweep_events) >= 1
        assert sweep_events[0].reference_swing.price == pytest.approx(1.0830)

    def test_no_sweep_if_close_below(self):
        """If the candle closes below the swing low, it's a BOS not a sweep."""
        ms = MarketStructure(swing_lookback=2, sweep_wick_ratio=0.4)
        setup = [
            _candle(0, 1.0860, 1.0865, 1.0855, 1.0858),
            _candle(1, 1.0858, 1.0862, 1.0840, 1.0842),
            _candle(2, 1.0842, 1.0848, 1.0830, 1.0845),
            _candle(3, 1.0845, 1.0860, 1.0843, 1.0858),
            _candle(4, 1.0858, 1.0865, 1.0850, 1.0862),
        ]
        for c in setup:
            ms.update(c)
        # Closes below the swing low - not a sweep
        break_candle = _candle(5, 1.0840, 1.0842, 1.0820, 1.0822)
        events = ms.update(break_candle)
        sweep_events = [e for e in events if e.kind is StructureKind.SWEEP_LOW]
        assert len(sweep_events) == 0


class TestBosChoch:
    def test_bos_bull_on_close_above_swing_high(self):
        ms = MarketStructure(swing_lookback=2)
        # Build a swing high at 1.0880
        setup = [
            _candle(0, 1.0850, 1.0855, 1.0845, 1.0852),
            _candle(1, 1.0852, 1.0860, 1.0848, 1.0858),
            _candle(2, 1.0858, 1.0880, 1.0855, 1.0875),  # swing high
            _candle(3, 1.0875, 1.0878, 1.0860, 1.0862),
            _candle(4, 1.0862, 1.0870, 1.0855, 1.0860),
        ]
        for c in setup:
            ms.update(c)

        # Close above the swing high -> BOS bull
        break_candle = _candle(5, 1.0870, 1.0895, 1.0868, 1.0890)
        events = ms.update(break_candle)
        bos = [e for e in events if e.kind is StructureKind.BOS_BULL]
        assert len(bos) >= 1
