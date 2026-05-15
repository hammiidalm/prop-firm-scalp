"""Tests for MarketStructure, HTFStructure and all new SMC primitives.

Coverage
--------
* Swing detection (fixed + adaptive)
* Liquidity sweep (high/low, positive + negative cases)
* BOS / CHOCH labelling
* Order Block: detection on bullish/bearish BOS, mitigation
* Fair Value Gap: bullish/bearish detection, fill, size filter
* HTFStructure: standalone bias tracking
* MarketStructure.set_htf_bias / get_htf_bias integration
* calculate_confluence_score: each factor independently + combined
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models import Candle
from app.strategy.market_structure import (
    FairValueGap,
    HTFBias,
    HTFStructure,
    MarketStructure,
    OrderBlock,
    StructureKind,
    SwingType,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_TS = datetime(2024, 6, 10, 8, 0, tzinfo=UTC)


def _c(
    idx: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    *,
    symbol: str = "EURUSD",
    timeframe: str = "M1",
) -> Candle:
    """Build a test candle at minute ``idx`` from _BASE_TS."""
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        timestamp=_BASE_TS + timedelta(minutes=idx),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100.0,
    )


def _feed(ms: MarketStructure | HTFStructure, candles: list[Candle]) -> list:
    """Feed all candles and return the flat list of all events."""
    all_events = []
    for c in candles:
        all_events.extend(ms.update(c))
    return all_events


# ---------------------------------------------------------------------------
# Shared candle sequences
# ---------------------------------------------------------------------------

def _swing_high_sequence(offset: int = 0) -> list[Candle]:
    """Five-candle sequence with a clear swing high at bar[2] (1.0880)."""
    return [
        _c(offset + 0, 1.0850, 1.0855, 1.0845, 1.0852),
        _c(offset + 1, 1.0852, 1.0858, 1.0848, 1.0855),
        _c(offset + 2, 1.0855, 1.0880, 1.0850, 1.0870),  # swing high @ 1.0880
        _c(offset + 3, 1.0870, 1.0875, 1.0860, 1.0862),
        _c(offset + 4, 1.0862, 1.0868, 1.0855, 1.0858),
    ]


def _swing_low_sequence(offset: int = 0) -> list[Candle]:
    """Five-candle sequence with a clear swing low at bar[2] (1.0830)."""
    return [
        _c(offset + 0, 1.0860, 1.0865, 1.0855, 1.0858),
        _c(offset + 1, 1.0858, 1.0862, 1.0840, 1.0842),
        _c(offset + 2, 1.0842, 1.0848, 1.0830, 1.0845),  # swing low @ 1.0830
        _c(offset + 3, 1.0845, 1.0860, 1.0843, 1.0858),
        _c(offset + 4, 1.0858, 1.0865, 1.0850, 1.0862),
    ]



# ===========================================================================
# TestSwingDetection
# ===========================================================================


class TestSwingDetection:
    """Basic swing high / low detection (adaptive_swing=False for clarity)."""

    def test_detects_swing_high(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False)
        _feed(ms, _swing_high_sequence())
        sh = ms.latest_swing_high()
        assert sh is not None
        assert sh.price == pytest.approx(1.0880)
        assert sh.type is SwingType.HIGH

    def test_detects_swing_low(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False)
        _feed(ms, _swing_low_sequence())
        sl = ms.latest_swing_low()
        assert sl is not None
        assert sl.price == pytest.approx(1.0830)
        assert sl.type is SwingType.LOW

    def test_no_swing_before_enough_bars(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False)
        # Only 4 bars fed — 2*2+1=5 required before first confirmation
        for c in _swing_high_sequence()[:4]:
            ms.update(c)
        assert ms.latest_swing_high() is None

    def test_prominence_field_is_positive(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False)
        _feed(ms, _swing_high_sequence())
        sh = ms.latest_swing_high()
        assert sh is not None
        assert sh.prominence >= 0.0

    def test_no_duplicate_swing_same_timestamp(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False)
        bars = _swing_high_sequence()
        # Feed the same closing bar twice (simulates a live tick update)
        _feed(ms, bars)
        prev_len = len(list(ms._swings))
        ms.update(bars[-1])          # duplicate timestamp
        assert len(list(ms._swings)) == prev_len


class TestAdaptiveSwing:
    """Adaptive prominence filter should suppress weak swings."""

    def test_prominent_swing_accepted(self):
        """A clearly dominant high (15 pips above neighbours) must pass."""
        ms = MarketStructure(swing_lookback=2, adaptive_swing=True,
                             prominence_atr_mult=0.3)
        _feed(ms, _swing_high_sequence())
        # Swing high is 1.0880, neighbours ~1.0868 → prominence ≈ 0.0012
        # ATR over 5 micro-bars is tiny (≈ 0.0005) so threshold is ~0.00015
        assert ms.latest_swing_high() is not None

    def test_weak_swing_rejected_when_threshold_high(self):
        """A very strict threshold should reject a borderline pivot."""
        ms = MarketStructure(swing_lookback=2, adaptive_swing=True,
                             prominence_atr_mult=5.0)  # unrealistically strict
        _feed(ms, _swing_high_sequence())
        # prominence ≈ 0.0012, ATR ≈ 0.0005 → threshold ≈ 0.0025 > prominence
        assert ms.latest_swing_high() is None


# ===========================================================================
# TestLiquiditySweep
# ===========================================================================


class TestLiquiditySweep:
    def test_sweep_low_detected(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             sweep_wick_ratio=0.4)
        _feed(ms, _swing_low_sequence())
        # Sweep: wick to 1.0825, close at 1.0850 (above swing low 1.0830)
        sweep_candle = _c(5, 1.0850, 1.0855, 1.0825, 1.0850)
        events = ms.update(sweep_candle)
        sweeps = [e for e in events if e.kind is StructureKind.SWEEP_LOW]
        assert len(sweeps) >= 1
        assert sweeps[0].reference_swing.price == pytest.approx(1.0830)

    def test_sweep_high_detected(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             sweep_wick_ratio=0.4)
        _feed(ms, _swing_high_sequence())
        # Sweep: wick to 1.0895, close at 1.0865 (below swing high 1.0880)
        sweep_candle = _c(5, 1.0865, 1.0895, 1.0860, 1.0865)
        events = ms.update(sweep_candle)
        sweeps = [e for e in events if e.kind is StructureKind.SWEEP_HIGH]
        assert len(sweeps) >= 1
        assert sweeps[0].reference_swing.price == pytest.approx(1.0880)

    def test_no_sweep_when_close_below_swing_low(self):
        """Close below the swing low → BOS, not a sweep."""
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             sweep_wick_ratio=0.4)
        _feed(ms, _swing_low_sequence())
        bos_candle = _c(5, 1.0840, 1.0842, 1.0815, 1.0818)
        events = ms.update(bos_candle)
        sweeps = [e for e in events if e.kind is StructureKind.SWEEP_LOW]
        assert len(sweeps) == 0

    def test_no_sweep_when_wick_too_small(self):
        """Wick below swing low but wick/range ratio below threshold → no sweep."""
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             sweep_wick_ratio=0.7)   # strict
        _feed(ms, _swing_low_sequence())
        # Small-wick candle: total range = 0.0020, wick below swing = 0.0005
        weak = _c(5, 1.0845, 1.0850, 1.0828, 1.0845)
        events = ms.update(weak)
        sweeps = [e for e in events if e.kind is StructureKind.SWEEP_LOW]
        assert len(sweeps) == 0


# ===========================================================================
# TestBosChoch
# ===========================================================================


class TestBosChoch:
    def test_bos_bull(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False)
        _feed(ms, _swing_high_sequence())
        bos = _c(5, 1.0870, 1.0895, 1.0868, 1.0890)   # close > 1.0880
        events = ms.update(bos)
        kinds = {e.kind for e in events}
        assert StructureKind.BOS_BULL in kinds

    def test_bos_bear(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False)
        _feed(ms, _swing_low_sequence())
        bos = _c(5, 1.0840, 1.0842, 1.0820, 1.0822)   # close < 1.0830
        events = ms.update(bos)
        kinds = {e.kind for e in events}
        assert StructureKind.BOS_BEAR in kinds

    def test_choch_bull_after_bearish_trend(self):
        """After a BOS_BEAR, the next upside break is labelled CHOCH_BULL."""
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False)
        # Build bearish structure
        _feed(ms, _swing_low_sequence())
        ms.update(_c(5, 1.0840, 1.0842, 1.0820, 1.0822))   # → BOS_BEAR
        assert ms.last_trend() is StructureKind.BOS_BEAR
        # Now build a swing high and break it
        _feed(ms, _swing_high_sequence(offset=6))
        choch = _c(11, 1.0870, 1.0895, 1.0868, 1.0892)
        events = ms.update(choch)
        kinds = {e.kind for e in events}
        assert StructureKind.CHOCH_BULL in kinds

    def test_last_trend_tracks_most_recent_bos(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False)
        _feed(ms, _swing_high_sequence())
        ms.update(_c(5, 1.0870, 1.0895, 1.0868, 1.0890))
        assert ms.last_trend() is StructureKind.BOS_BULL



# ===========================================================================
# TestOrderBlock
# ===========================================================================


def _build_bullish_ob_sequence() -> list[Candle]:
    """Build a sequence that triggers a Bullish OB.

    Pattern:
      - Bars 0-4: establish a swing low at 1.0830.
      - Bar 5: big bearish candle (will become the OB).
      - Bar 6: massive bullish displacement that breaks the swing high
               (body > 1.5 × ATR) → should produce a BULLISH OB whose
               zone is bar 5's body.
    """
    return [
        # Build swing low at 1.0830
        _c(0, 1.0860, 1.0865, 1.0855, 1.0858),
        _c(1, 1.0858, 1.0862, 1.0840, 1.0842),
        _c(2, 1.0842, 1.0848, 1.0830, 1.0845),   # swing low
        _c(3, 1.0845, 1.0862, 1.0843, 1.0860),
        _c(4, 1.0860, 1.0868, 1.0852, 1.0856),
        # Bar 5: bearish OB candle (open 1.0856, close 1.0838)
        _c(5, 1.0856, 1.0858, 1.0836, 1.0838),
        # Bar 6: impulse bull BOS — close breaks above recent high 1.0868
        # body = 0.0052 which should exceed 1.5 × ATR of ~0.0008
        _c(6, 1.0838, 1.0900, 1.0836, 1.0890),
    ]


def _build_bearish_ob_sequence() -> list[Candle]:
    """Sequence that triggers a Bearish OB.

    Pattern:
      - Bars 0-4: establish a swing high at 1.0880.
      - Bar 5: big bullish candle (will become the OB).
      - Bar 6: massive bearish displacement → BEARISH OB.
    """
    return [
        _c(0, 1.0850, 1.0855, 1.0845, 1.0852),
        _c(1, 1.0852, 1.0858, 1.0848, 1.0855),
        _c(2, 1.0855, 1.0880, 1.0850, 1.0870),   # swing high
        _c(3, 1.0870, 1.0875, 1.0860, 1.0862),
        _c(4, 1.0862, 1.0868, 1.0855, 1.0858),
        # Bar 5: bullish OB candle
        _c(5, 1.0858, 1.0878, 1.0856, 1.0876),
        # Bar 6: impulse bear BOS — close breaks below swing low 1.0830
        _c(6, 1.0876, 1.0877, 1.0818, 1.0822),
    ]


class TestOrderBlock:
    def test_bullish_ob_detected_on_bos(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             ob_displacement_mult=1.5)
        _feed(ms, _build_bullish_ob_sequence())
        obs = ms.find_order_blocks(direction="BULLISH", active_only=True)
        assert len(obs) >= 1, "Expected at least one active bullish OB"
        ob = obs[0]
        assert ob.direction == "BULLISH"
        assert ob.zone_high > ob.zone_low
        assert not ob.mitigated

    def test_bearish_ob_detected_on_bos(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             ob_displacement_mult=1.5)
        _feed(ms, _build_bearish_ob_sequence())
        obs = ms.find_order_blocks(direction="BEARISH", active_only=True)
        assert len(obs) >= 1, "Expected at least one active bearish OB"
        ob = obs[0]
        assert ob.direction == "BEARISH"
        assert not ob.mitigated

    def test_bullish_ob_zone_is_bearish_candle_body(self):
        """The OB zone must be the body (open/close) of the last bearish candle."""
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             ob_displacement_mult=1.5)
        _feed(ms, _build_bullish_ob_sequence())
        obs = ms.find_order_blocks(direction="BULLISH", active_only=True)
        assert obs, "No OB found"
        ob = obs[0]
        # Bar 5 is the OB: open=1.0856, close=1.0838
        assert ob.zone_high == pytest.approx(1.0856, abs=1e-5)
        assert ob.zone_low == pytest.approx(1.0838, abs=1e-5)

    def test_bullish_ob_mitigated_when_price_revisits(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             ob_displacement_mult=1.5)
        seq = _build_bullish_ob_sequence()
        _feed(ms, seq)
        obs_before = ms.find_order_blocks(direction="BULLISH", active_only=True)
        assert obs_before

        # Price pulls back into the OB zone (low ≤ zone_high)
        ob = obs_before[0]
        mitigation_candle = _c(
            7, ob.zone_high + 0.0005, ob.zone_high + 0.0010,
            ob.zone_low - 0.0002, ob.zone_high - 0.0001,
        )
        ms.update(mitigation_candle)

        obs_after = ms.find_order_blocks(direction="BULLISH", active_only=True)
        # The OB we found before should now be mitigated
        mitigated_ids = {o.timestamp for o in ms.find_order_blocks(
            direction="BULLISH", active_only=False) if o.mitigated}
        assert ob.timestamp in mitigated_ids

    def test_bearish_ob_mitigated_when_price_revisits(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             ob_displacement_mult=1.5)
        _feed(ms, _build_bearish_ob_sequence())
        obs = ms.find_order_blocks(direction="BEARISH", active_only=True)
        assert obs
        ob = obs[0]

        # Price rallies back into supply zone (high ≥ zone_low)
        mitigation_candle = _c(
            7, ob.zone_low - 0.0005, ob.zone_low + 0.0002,
            ob.zone_low - 0.0010, ob.zone_low - 0.0003,
        )
        ms.update(mitigation_candle)

        mitigated = {o.timestamp for o in ms.find_order_blocks(
            direction="BEARISH", active_only=False) if o.mitigated}
        assert ob.timestamp in mitigated

    def test_displacement_too_small_no_ob(self):
        """An impulsive candle smaller than ob_displacement_mult × ATR
        must NOT create an order block."""
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             ob_displacement_mult=99.0)  # impossible threshold
        _feed(ms, _build_bullish_ob_sequence())
        assert ms.find_order_blocks(active_only=True) == []

    def test_find_order_blocks_direction_filter(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             ob_displacement_mult=1.5)
        _feed(ms, _build_bullish_ob_sequence())
        assert ms.find_order_blocks(direction="BULLISH") != []
        assert ms.find_order_blocks(direction="BEARISH") == []

    def test_find_order_blocks_active_only_false_includes_mitigated(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             ob_displacement_mult=1.5)
        seq = _build_bullish_ob_sequence()
        _feed(ms, seq)
        obs = ms.find_order_blocks(direction="BULLISH", active_only=True)
        ob = obs[0]
        # Mitigate it
        mit = _c(7, ob.zone_high, ob.zone_high + 0.001,
                 ob.zone_low - 0.001, ob.zone_high)
        ms.update(mit)
        all_obs = ms.find_order_blocks(direction="BULLISH", active_only=False)
        assert any(o.timestamp == ob.timestamp for o in all_obs)



# ===========================================================================
# TestFairValueGap
# ===========================================================================


def _bullish_fvg_trio(offset: int = 0) -> tuple[Candle, Candle, Candle]:
    """Three candles forming a clear bullish FVG.

    bar[0].high (1.0850) < bar[2].low (1.0870) → 20-pip gap.
    """
    b0 = _c(offset + 0, 1.0840, 1.0850, 1.0835, 1.0848)  # high = 1.0850
    b1 = _c(offset + 1, 1.0855, 1.0895, 1.0852, 1.0890)  # displacement
    b2 = _c(offset + 2, 1.0875, 1.0900, 1.0870, 1.0898)  # low  = 1.0870
    return b0, b1, b2


def _bearish_fvg_trio(offset: int = 0) -> tuple[Candle, Candle, Candle]:
    """Three candles forming a clear bearish FVG.

    bar[0].low (1.0870) > bar[2].high (1.0850) → 20-pip gap.
    """
    b0 = _c(offset + 0, 1.0880, 1.0885, 1.0870, 1.0872)  # low  = 1.0870
    b1 = _c(offset + 1, 1.0865, 1.0868, 1.0825, 1.0830)  # displacement
    b2 = _c(offset + 2, 1.0835, 1.0850, 1.0820, 1.0825)  # high = 1.0850
    return b0, b1, b2


class TestFairValueGap:
    def test_bullish_fvg_detected(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             min_fvg_atr_mult=0.0)   # accept all sizes
        b0, b1, b2 = _bullish_fvg_trio()
        for c in (b0, b1, b2):
            ms.update(c)
        fvgs = ms.find_fvg(direction="BULLISH", active_only=True)
        assert len(fvgs) >= 1
        fvg = fvgs[0]
        assert fvg.direction == "BULLISH"
        assert fvg.gap_low == pytest.approx(b0.high, abs=1e-6)
        assert fvg.gap_high == pytest.approx(b2.low, abs=1e-6)
        assert not fvg.filled

    def test_bearish_fvg_detected(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             min_fvg_atr_mult=0.0)
        b0, b1, b2 = _bearish_fvg_trio()
        for c in (b0, b1, b2):
            ms.update(c)
        fvgs = ms.find_fvg(direction="BEARISH", active_only=True)
        assert len(fvgs) >= 1
        fvg = fvgs[0]
        assert fvg.direction == "BEARISH"
        assert fvg.gap_high == pytest.approx(b0.low, abs=1e-6)
        assert fvg.gap_low == pytest.approx(b2.high, abs=1e-6)

    def test_fvg_size_and_midpoint(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             min_fvg_atr_mult=0.0)
        b0, b1, b2 = _bullish_fvg_trio()
        for c in (b0, b1, b2):
            ms.update(c)
        fvg = ms.find_fvg(direction="BULLISH")[0]
        expected_size = b2.low - b0.high
        assert fvg.size == pytest.approx(expected_size, abs=1e-7)
        assert fvg.midpoint == pytest.approx(
            (b2.low + b0.high) / 2.0, abs=1e-7
        )

    def test_bullish_fvg_filled_when_price_drops_through(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             min_fvg_atr_mult=0.0)
        b0, b1, b2 = _bullish_fvg_trio()
        for c in (b0, b1, b2):
            ms.update(c)
        fvg_before = ms.find_fvg(direction="BULLISH")[0]

        # Candle whose low ≤ fvg.gap_low fills the gap
        fill_candle = _c(3, 1.0870, 1.0872, fvg_before.gap_low - 0.0001, 1.0868)
        ms.update(fill_candle)

        all_fvgs = ms.find_fvg(direction="BULLISH", active_only=False)
        filled = [f for f in all_fvgs if f.timestamp == fvg_before.timestamp]
        assert filled and filled[0].filled

    def test_bearish_fvg_filled_when_price_rallies_through(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             min_fvg_atr_mult=0.0)
        b0, b1, b2 = _bearish_fvg_trio()
        for c in (b0, b1, b2):
            ms.update(c)
        fvg_before = ms.find_fvg(direction="BEARISH")[0]

        fill_candle = _c(3, 1.0830, fvg_before.gap_high + 0.0001, 1.0828, 1.0835)
        ms.update(fill_candle)

        all_fvgs = ms.find_fvg(direction="BEARISH", active_only=False)
        filled = [f for f in all_fvgs if f.timestamp == fvg_before.timestamp]
        assert filled and filled[0].filled

    def test_fvg_not_detected_when_bars_overlap(self):
        """No gap if bar[0].high ≥ bar[2].low (bars overlap)."""
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             min_fvg_atr_mult=0.0)
        # bar[0].high == bar[2].low → no gap
        b0 = _c(0, 1.0840, 1.0870, 1.0835, 1.0865)
        b1 = _c(1, 1.0868, 1.0890, 1.0865, 1.0885)
        b2 = _c(2, 1.0882, 1.0895, 1.0870, 1.0892)  # low == b0.high
        for c in (b0, b1, b2):
            ms.update(c)
        assert ms.find_fvg(direction="BULLISH") == []

    def test_fvg_size_filter_rejects_tiny_gap(self):
        """Gaps below min_fvg_atr_mult × ATR must be discarded."""
        # Force a non-zero ATR by using a wider price range then test
        # that a micro-gap is rejected when the multiplier is large.
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             min_fvg_atr_mult=100.0)  # absurdly strict
        b0, b1, b2 = _bullish_fvg_trio()
        for c in (b0, b1, b2):
            ms.update(c)
        # ATR will be very small (all bars in a tight range) so even a
        # real gap gets filtered by the crazy multiplier
        assert ms.find_fvg(direction="BULLISH") == []

    def test_find_fvg_direction_filter(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             min_fvg_atr_mult=0.0)
        b0, b1, b2 = _bullish_fvg_trio()
        for c in (b0, b1, b2):
            ms.update(c)
        assert ms.find_fvg(direction="BULLISH") != []
        assert ms.find_fvg(direction="BEARISH") == []



# ===========================================================================
# TestHTFStructure
# ===========================================================================


class TestHTFStructure:
    """HTFStructure operates independently of MarketStructure."""

    def test_initial_bias_is_neutral(self):
        htf = HTFStructure(swing_lookback=2)
        assert htf.bias is HTFBias.NEUTRAL

    def test_bias_becomes_bullish_after_bos_bull(self):
        htf = HTFStructure(swing_lookback=2)
        # Build swing high then break it
        _feed(htf, _swing_high_sequence())
        htf.update(_c(5, 1.0870, 1.0895, 1.0868, 1.0890))
        assert htf.bias is HTFBias.BULLISH

    def test_bias_becomes_bearish_after_bos_bear(self):
        htf = HTFStructure(swing_lookback=2)
        _feed(htf, _swing_low_sequence())
        htf.update(_c(5, 1.0840, 1.0842, 1.0820, 1.0822))
        assert htf.bias is HTFBias.BEARISH

    def test_bias_flips_on_choch(self):
        htf = HTFStructure(swing_lookback=2)
        # First: bearish BOS
        _feed(htf, _swing_low_sequence())
        htf.update(_c(5, 1.0840, 1.0842, 1.0820, 1.0822))
        assert htf.bias is HTFBias.BEARISH
        # Now: bullish CHOCH (break above swing high → CHOCH_BULL)
        _feed(htf, _swing_high_sequence(offset=6))
        htf.update(_c(11, 1.0870, 1.0895, 1.0868, 1.0892))
        assert htf.bias is HTFBias.BULLISH

    def test_htf_returns_swing_points(self):
        htf = HTFStructure(swing_lookback=2)
        _feed(htf, _swing_high_sequence())
        assert htf.latest_swing_high() is not None
        assert htf.latest_swing_high().price == pytest.approx(1.0880)

    def test_htf_detects_sweep(self):
        htf = HTFStructure(swing_lookback=2, sweep_wick_ratio=0.4)
        _feed(htf, _swing_low_sequence())
        sweep = _c(5, 1.0850, 1.0855, 1.0825, 1.0850)
        events = htf.update(sweep)
        assert any(e.kind is StructureKind.SWEEP_LOW for e in events)

    def test_htf_runs_independently_from_ltf(self):
        """Updating HTFStructure must not affect a separate MarketStructure."""
        htf = HTFStructure()
        ltf = MarketStructure(swing_lookback=2, adaptive_swing=False)
        _feed(htf, _swing_high_sequence())
        htf.update(_c(5, 1.0870, 1.0895, 1.0868, 1.0890))
        # LTF has received no candles
        assert ltf.latest_swing_high() is None
        assert ltf.last_trend() is None


# ===========================================================================
# TestHTFBiasIntegration
# ===========================================================================


class TestHTFBiasIntegration:
    """set_htf_bias / get_htf_bias on MarketStructure."""

    def test_default_htf_bias_is_neutral(self):
        ms = MarketStructure()
        assert ms.get_htf_bias() is HTFBias.NEUTRAL

    def test_set_and_get_htf_bias(self):
        ms = MarketStructure()
        ms.set_htf_bias(HTFBias.BULLISH)
        assert ms.get_htf_bias() is HTFBias.BULLISH

    def test_set_htf_bias_overrides_previous(self):
        ms = MarketStructure()
        ms.set_htf_bias(HTFBias.BULLISH)
        ms.set_htf_bias(HTFBias.BEARISH)
        assert ms.get_htf_bias() is HTFBias.BEARISH

    def test_htf_bias_injected_from_htf_structure(self):
        """Full integration: HTFStructure feeds bias into MarketStructure."""
        htf = HTFStructure(swing_lookback=2)
        ltf = MarketStructure(swing_lookback=2, adaptive_swing=False)

        # Build bullish bias on HTF
        _feed(htf, _swing_high_sequence())
        htf.update(_c(5, 1.0870, 1.0895, 1.0868, 1.0890))
        assert htf.bias is HTFBias.BULLISH

        # Inject into LTF
        ltf.set_htf_bias(htf.bias)
        assert ltf.get_htf_bias() is HTFBias.BULLISH

    def test_ltf_does_not_change_htf_bias_autonomously(self):
        """LTF candles must NOT modify _htf_bias."""
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False)
        ms.set_htf_bias(HTFBias.BEARISH)
        # Feed a lot of bullish LTF bars
        for c in _swing_high_sequence():
            ms.update(c)
        ms.update(_c(5, 1.0870, 1.0895, 1.0868, 1.0890))   # LTF BOS_BULL
        # HTF bias must still be whatever we injected
        assert ms.get_htf_bias() is HTFBias.BEARISH



# ===========================================================================
# TestConfluenceScore
# ===========================================================================


class TestConfluenceScore:
    """Each factor is exercised in isolation then combined."""

    # ------------------------------------------------------------------
    # Factor 1: HTF bias
    # ------------------------------------------------------------------

    def test_htf_aligned_adds_025(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False)
        ms.set_htf_bias(HTFBias.BULLISH)
        score = ms.calculate_confluence_score(1.0850, "BULLISH")
        assert score == pytest.approx(0.25, abs=0.01)

    def test_htf_opposed_adds_nothing(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False)
        ms.set_htf_bias(HTFBias.BEARISH)
        score = ms.calculate_confluence_score(1.0850, "BULLISH")
        assert score == pytest.approx(0.0, abs=0.01)

    def test_htf_neutral_adds_nothing(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False)
        assert ms.get_htf_bias() is HTFBias.NEUTRAL
        score = ms.calculate_confluence_score(1.0850, "BULLISH")
        assert score == pytest.approx(0.0, abs=0.01)

    # ------------------------------------------------------------------
    # Factor 2: Order block at price
    # ------------------------------------------------------------------

    def test_ob_at_price_adds_025(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             ob_displacement_mult=1.5)
        _feed(ms, _build_bullish_ob_sequence())
        obs = ms.find_order_blocks(direction="BULLISH", active_only=True)
        assert obs, "Pre-condition: need an OB"
        ob = obs[0]
        price_in_zone = (ob.zone_high + ob.zone_low) / 2.0
        score = ms.calculate_confluence_score(price_in_zone, "BULLISH")
        assert score >= 0.25 - 1e-9

    def test_ob_outside_price_adds_nothing(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             ob_displacement_mult=1.5)
        _feed(ms, _build_bullish_ob_sequence())
        # Price way above any OB zone
        score = ms.calculate_confluence_score(1.2000, "BULLISH")
        # Only structure factor could contribute (possibly 0.20 from BOS)
        assert score <= 0.20 + 1e-9

    # ------------------------------------------------------------------
    # Factor 3: FVG at price
    # ------------------------------------------------------------------

    def test_fvg_at_price_adds_020(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             min_fvg_atr_mult=0.0)
        b0, b1, b2 = _bullish_fvg_trio()
        for c in (b0, b1, b2):
            ms.update(c)
        fvgs = ms.find_fvg(direction="BULLISH")
        assert fvgs
        price_in_gap = fvgs[0].midpoint
        score = ms.calculate_confluence_score(price_in_gap, "BULLISH")
        assert score >= 0.20 - 1e-9

    def test_filled_fvg_excluded_from_score(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             min_fvg_atr_mult=0.0)
        b0, b1, b2 = _bullish_fvg_trio()
        for c in (b0, b1, b2):
            ms.update(c)
        fvg = ms.find_fvg(direction="BULLISH")[0]
        price_in_gap = fvg.midpoint
        # Fill the gap
        ms.update(_c(3, fvg.gap_high, fvg.gap_high + 0.001,
                     fvg.gap_low - 0.001, fvg.gap_high))
        # FVG factor should now be 0
        score_after = ms.calculate_confluence_score(price_in_gap, "BULLISH")
        assert score_after < 0.20  # filled FVG not counted

    # ------------------------------------------------------------------
    # Factor 4: LTF structure alignment
    # ------------------------------------------------------------------

    def test_ltf_bos_bull_adds_020(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False)
        _feed(ms, _swing_high_sequence())
        ms.update(_c(5, 1.0870, 1.0895, 1.0868, 1.0890))   # BOS_BULL
        assert ms.last_trend() is StructureKind.BOS_BULL
        score = ms.calculate_confluence_score(1.0860, "BULLISH")
        assert score >= 0.20 - 1e-9

    def test_ltf_bos_bear_adds_020_for_short(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False)
        _feed(ms, _swing_low_sequence())
        ms.update(_c(5, 1.0840, 1.0842, 1.0815, 1.0818))   # BOS_BEAR
        assert ms.last_trend() is StructureKind.BOS_BEAR
        score = ms.calculate_confluence_score(1.0840, "BEARISH")
        assert score >= 0.20 - 1e-9

    def test_opposing_structure_adds_nothing(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False)
        _feed(ms, _swing_high_sequence())
        ms.update(_c(5, 1.0870, 1.0895, 1.0868, 1.0890))   # BOS_BULL
        # Asking for BEARISH entry — structure is misaligned
        score = ms.calculate_confluence_score(1.0870, "BEARISH")
        assert score < 0.20

    # ------------------------------------------------------------------
    # Factor 5: Swing proximity
    # ------------------------------------------------------------------

    def test_swing_proximity_adds_010(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False)
        _feed(ms, _swing_low_sequence())
        sl = ms.latest_swing_low()
        assert sl is not None
        # Price right at the swing low → proximity factor fires
        score = ms.calculate_confluence_score(sl.price, "BULLISH")
        assert score >= 0.10 - 1e-9

    def test_swing_far_away_no_proximity(self):
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False)
        _feed(ms, _swing_low_sequence())
        # Price 200 pips from any swing
        score = ms.calculate_confluence_score(1.0500, "BULLISH")
        # Should be 0 (no HTF, no OB, no FVG, no structure, no proximity)
        assert score == pytest.approx(0.0, abs=0.01)

    # ------------------------------------------------------------------
    # Combined: all factors
    # ------------------------------------------------------------------

    def test_maximum_score_near_10(self):
        """With every factor active the score must reach at least 0.90."""
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             ob_displacement_mult=1.5,
                             min_fvg_atr_mult=0.0)

        # 1. Set HTF bullish bias (+0.25)
        ms.set_htf_bias(HTFBias.BULLISH)

        # 2. Build a bullish OB
        _feed(ms, _build_bullish_ob_sequence())
        obs = ms.find_order_blocks(direction="BULLISH", active_only=True)
        assert obs, "Need at least one active OB for this test"
        ob = obs[0]
        price = (ob.zone_high + ob.zone_low) / 2.0

        # 3. LTF structure should now be bullish from the BOS in that sequence
        # (BOS_BULL fires on bar 6 of _build_bullish_ob_sequence)

        # 4. Create a bullish FVG that overlaps the test price by injecting
        #    three synthetic bars around the OB midpoint
        gap_low = price - 0.0002
        gap_high = price + 0.0002
        ms._fvgs.append(FairValueGap(
            direction="BULLISH",
            gap_low=gap_low,
            gap_high=gap_high,
            timestamp=_BASE_TS + timedelta(minutes=100),
        ))

        # 5. Make swing low sit near our price so proximity fires
        from app.strategy.market_structure import SwingPoint, SwingType
        ms._swings.append(SwingPoint(
            type=SwingType.LOW,
            price=price - 0.0001,
            timestamp=_BASE_TS + timedelta(minutes=99),
            index=99,
            prominence=0.001,
        ))

        score = ms.calculate_confluence_score(price, "BULLISH")
        assert score >= 0.90

    def test_score_capped_at_10(self):
        """Score must never exceed 1.0 even when all factors contribute."""
        ms = MarketStructure(swing_lookback=2, adaptive_swing=False,
                             ob_displacement_mult=1.5,
                             min_fvg_atr_mult=0.0)
        ms.set_htf_bias(HTFBias.BULLISH)
        _feed(ms, _build_bullish_ob_sequence())
        obs = ms.find_order_blocks(direction="BULLISH", active_only=True)
        if not obs:
            pytest.skip("no OB detected — skip cap test")
        price = (obs[0].zone_high + obs[0].zone_low) / 2.0
        score = ms.calculate_confluence_score(price, "BULLISH")
        assert score <= 1.0
