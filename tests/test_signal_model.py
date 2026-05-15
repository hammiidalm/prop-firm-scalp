"""Tests for Signal model creation and validation."""

from datetime import UTC, datetime

import pytest

from app.models import Signal


class TestSignalLong:
    def test_valid_long(self):
        sig = Signal.long(
            symbol="EURUSD",
            timeframe="M1",
            entry=1.0850,
            sl=1.0840,
            tp=1.0870,
            reason="test",
            session="LONDON",
            generated_at=datetime(2024, 6, 10, 8, 30, tzinfo=UTC),
        )
        assert sig.risk_distance == pytest.approx(0.0010, abs=1e-8)
        assert sig.reward_distance == pytest.approx(0.0020, abs=1e-8)
        assert sig.rr_ratio == pytest.approx(2.0, abs=0.01)

    def test_invalid_long_sl_above_entry(self):
        with pytest.raises(ValueError, match="LONG requires sl < entry < tp"):
            Signal.long(
                symbol="EURUSD",
                timeframe="M1",
                entry=1.0850,
                sl=1.0860,  # SL above entry!
                tp=1.0870,
                reason="test",
                session="LONDON",
                generated_at=datetime(2024, 6, 10, 8, 30, tzinfo=UTC),
            )


class TestSignalShort:
    def test_valid_short(self):
        sig = Signal.short(
            symbol="EURUSD",
            timeframe="M1",
            entry=1.0850,
            sl=1.0860,
            tp=1.0830,
            reason="test",
            session="LONDON",
            generated_at=datetime(2024, 6, 10, 8, 30, tzinfo=UTC),
        )
        assert sig.risk_distance == pytest.approx(0.0010, abs=1e-8)
        assert sig.reward_distance == pytest.approx(0.0020, abs=1e-8)
        assert sig.rr_ratio == pytest.approx(2.0, abs=0.01)

    def test_invalid_short_tp_above_entry(self):
        with pytest.raises(ValueError, match="SHORT requires tp < entry < sl"):
            Signal.short(
                symbol="EURUSD",
                timeframe="M1",
                entry=1.0850,
                sl=1.0860,
                tp=1.0870,  # TP above entry for short!
                reason="test",
                session="LONDON",
                generated_at=datetime(2024, 6, 10, 8, 30, tzinfo=UTC),
            )
