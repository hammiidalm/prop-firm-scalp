"""Shared fixtures for the test suite."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config.settings import Settings, TradingMode
from app.models import Candle
from app.utils.sessions import SessionFilter


@pytest.fixture
def settings() -> Settings:
    """Return a test-friendly Settings instance (paper mode, fast params)."""
    return Settings(
        app_mode=TradingMode.paper,
        app_env="development",
        log_json=False,
        log_level="DEBUG",
        symbols=["EURUSD"],
        account_balance=100_000.0,
        risk_per_trade_pct=0.0035,
        max_daily_loss_pct=0.01,
        max_trades_per_day=5,
        max_consecutive_losses=3,
        target_profit_pct_min=0.001,
        target_profit_pct_max=0.002,
        max_spread_pips_fx=1.5,
        max_spread_pips_metals=35.0,
        database_url="sqlite+aiosqlite:///./data/test_journal.db",
    )


@pytest.fixture
def session_filter() -> SessionFilter:
    return SessionFilter(
        london_open_utc=7,
        london_close_utc=11,
        ny_open_utc=12,
        ny_close_utc=16,
    )


@pytest.fixture
def make_candle():
    """Factory fixture for creating test candles."""

    def _make(
        *,
        symbol: str = "EURUSD",
        timeframe: str = "M1",
        timestamp: datetime | None = None,
        open: float = 1.0850,
        high: float = 1.0855,
        low: float = 1.0845,
        close: float = 1.0852,
        volume: float = 100.0,
    ) -> Candle:
        ts = timestamp or datetime(2024, 6, 10, 8, 30, 0, tzinfo=UTC)
        return Candle(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=ts,
            open=open,
            high=high,
            low=low,
            close=close,
            volume=volume,
        )

    return _make
