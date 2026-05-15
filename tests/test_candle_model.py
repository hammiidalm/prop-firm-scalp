"""Tests for the Candle model and CandleSeries."""

from datetime import UTC, datetime, timedelta

import pytest

from app.models import Candle, CandleSeries


class TestCandle:
    def test_bullish_candle(self, make_candle):
        c = make_candle(open=1.0850, close=1.0860, high=1.0865, low=1.0845)
        assert c.is_bullish
        assert not c.is_bearish
        assert c.body == pytest.approx(0.0010, abs=1e-8)
        assert c.range == pytest.approx(0.0020, abs=1e-8)

    def test_bearish_candle(self, make_candle):
        c = make_candle(open=1.0860, close=1.0850, high=1.0865, low=1.0845)
        assert c.is_bearish
        assert not c.is_bullish

    def test_upper_lower_wicks(self, make_candle):
        c = make_candle(open=1.0850, close=1.0855, high=1.0870, low=1.0830)
        # upper wick = 1.0870 - max(1.0855, 1.0850) = 0.0015
        assert c.upper_wick == pytest.approx(0.0015, abs=1e-8)
        # lower wick = min(1.0855, 1.0850) - 1.0830 = 0.0020
        assert c.lower_wick == pytest.approx(0.0020, abs=1e-8)

    def test_validation_high_ge_open(self):
        with pytest.raises(Exception):
            Candle(
                symbol="EURUSD",
                timeframe="M1",
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
                open=1.10,
                high=1.05,  # less than open - invalid
                low=1.00,
                close=1.08,
            )

    def test_frozen_immutability(self, make_candle):
        c = make_candle()
        with pytest.raises(Exception):
            c.close = 999.0  # type: ignore[misc]


class TestCandleSeries:
    def test_append_and_length(self, make_candle):
        series = CandleSeries("EURUSD", "M1", maxlen=100)
        ts = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
        for i in range(10):
            c = make_candle(timestamp=ts + timedelta(minutes=i))
            series.append(c)
        assert len(series) == 10

    def test_replace_same_timestamp(self, make_candle):
        series = CandleSeries("EURUSD", "M1")
        ts = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
        c1 = make_candle(timestamp=ts, close=1.0850)
        c2 = make_candle(timestamp=ts, close=1.0860)
        series.append(c1)
        series.append(c2)
        assert len(series) == 1
        assert series.last.close == pytest.approx(1.0860)

    def test_maxlen_eviction(self, make_candle):
        series = CandleSeries("EURUSD", "M1", maxlen=5)
        ts = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
        for i in range(10):
            series.append(make_candle(timestamp=ts + timedelta(minutes=i)))
        assert len(series) == 5

    def test_wrong_symbol_raises(self, make_candle):
        series = CandleSeries("EURUSD", "M1")
        c = make_candle(symbol="XAUUSD")
        with pytest.raises(ValueError):
            series.append(c)

    def test_tail(self, make_candle):
        series = CandleSeries("EURUSD", "M1")
        ts = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
        for i in range(20):
            series.append(make_candle(timestamp=ts + timedelta(minutes=i)))
        tail = series.tail(5)
        assert len(tail) == 5
        assert tail[-1].timestamp == ts + timedelta(minutes=19)
