"""Tests for the RiskManager."""

from datetime import UTC, datetime

import pytest

from app.models import Signal, Trade, TradeStatus
from app.models.signal import SignalDirection
from app.risk import RiskManager, RiskRejectReason


def _make_signal(entry: float = 1.0850, sl: float = 1.0840, tp: float = 1.0870) -> Signal:
    return Signal.long(
        symbol="EURUSD",
        timeframe="M1",
        entry=entry,
        sl=sl,
        tp=tp,
        reason="test",
        session="LONDON",
        generated_at=datetime(2024, 6, 10, 8, 30, tzinfo=UTC),
    )


def _make_losing_trade(pnl: float = -35.0) -> Trade:
    return Trade(
        trade_id="t1",
        symbol="EURUSD",
        direction=SignalDirection.LONG,
        entry_time=datetime(2024, 6, 10, 8, 30, tzinfo=UTC),
        entry_price=1.0850,
        stop_loss=1.0840,
        take_profit=1.0870,
        quantity=0.10,
        exit_price=1.0840,
        pnl=pnl,
        status=TradeStatus.CLOSED_LOSS,
        session="LONDON",
        entry_reason="test",
        exit_reason="stop_loss",
    )


class TestRiskManagerBasic:
    def test_accept_valid_signal(self, settings):
        rm = RiskManager(settings=settings, starting_balance=100_000.0)
        decision = rm.evaluate(_make_signal())
        assert decision.accepted
        assert decision.reason is RiskRejectReason.OK
        assert decision.quantity_lots > 0

    def test_reject_after_daily_trade_limit(self, settings):
        settings.max_trades_per_day = 2
        rm = RiskManager(settings=settings, starting_balance=100_000.0)
        rm._trades_today = 2
        decision = rm.evaluate(_make_signal())
        assert not decision.accepted
        assert decision.reason is RiskRejectReason.DAILY_TRADE_LIMIT

    def test_reject_after_consecutive_losses(self, settings):
        settings.max_consecutive_losses = 3
        rm = RiskManager(settings=settings, starting_balance=100_000.0)
        # Simulate 3 losses
        for _ in range(3):
            rm.register_trade_close(_make_losing_trade())
        decision = rm.evaluate(_make_signal())
        assert not decision.accepted
        assert decision.reason is RiskRejectReason.CONSECUTIVE_LOSSES

    def test_consecutive_loss_reset_on_win(self, settings):
        rm = RiskManager(settings=settings, starting_balance=100_000.0)
        # 2 losses
        rm.register_trade_close(_make_losing_trade())
        rm.register_trade_close(_make_losing_trade())
        # 1 win resets counter
        win = Trade(
            trade_id="w1",
            symbol="EURUSD",
            direction=SignalDirection.LONG,
            entry_time=datetime(2024, 6, 10, 8, 30, tzinfo=UTC),
            entry_price=1.0850,
            stop_loss=1.0840,
            take_profit=1.0870,
            quantity=0.10,
            pnl=50.0,
            status=TradeStatus.CLOSED_WIN,
            session="LONDON",
            entry_reason="test",
            exit_reason="take_profit",
        )
        rm.register_trade_close(win)
        assert rm._consecutive_losses == 0

    def test_reject_daily_loss_limit(self, settings):
        settings.max_daily_loss_pct = 0.01  # 1% = $1000
        rm = RiskManager(settings=settings, starting_balance=100_000.0)
        # A single catastrophic loss exceeding the daily limit
        rm.register_trade_close(_make_losing_trade(pnl=-1100.0))
        decision = rm.evaluate(_make_signal())
        assert not decision.accepted
        assert decision.reason is RiskRejectReason.DAILY_LOSS_LIMIT

    def test_reject_spread_too_wide(self, settings):
        rm = RiskManager(settings=settings, starting_balance=100_000.0)
        decision = rm.evaluate(_make_signal(), spread_pips=5.0)  # cap is 1.5
        assert not decision.accepted
        assert decision.reason is RiskRejectReason.SPREAD_TOO_WIDE


class TestPositionSizing:
    def test_size_within_risk_budget(self, settings):
        settings.risk_per_trade_pct = 0.0035  # 0.35% = $350 on 100k
        rm = RiskManager(settings=settings, starting_balance=100_000.0)
        signal = _make_signal(entry=1.0850, sl=1.0840, tp=1.0870)
        # Risk distance = 10 pips, $10/pip/lot -> max lots = $350 / ($10 * 10) = 3.5
        decision = rm.evaluate(signal)
        assert decision.accepted
        assert 3.0 <= decision.quantity_lots <= 3.5

    def test_zero_risk_distance_rejected(self, settings):
        rm = RiskManager(settings=settings, starting_balance=100_000.0)
        # SL == entry -> risk distance is 0 -> should be rejected by Signal validation
        with pytest.raises(ValueError):
            _make_signal(entry=1.0850, sl=1.0850, tp=1.0870)
