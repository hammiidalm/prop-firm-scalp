"""Backtesting engine - replay historical candles through a strategy.

Design philosophy
-----------------
* The engine is a pure async loop that feeds candles from a DataFrame or CSV
  to the strategy, routes signals through the risk manager, and simulates
  fills via the ``PaperBroker``.
* No look-ahead bias: the strategy only ever sees bars up to (and including)
  the current timestamp.
* Spread and slippage can be injected per-bar to stress-test realistic
  conditions.
* Results are collected in a ``BacktestResult`` dataclass that surfaces
  winrate, equity curve, max drawdown, session stats, and per-trade records.

Usage example (see ``scripts/run_backtest.py`` for a full script):

    candles = load_candles_from_csv("data/EURUSD_M1.csv")
    engine = BacktestEngine(settings=get_settings(), candles=candles)
    result = await engine.run()
    print(result.summary())
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

import polars as pl

from app.analytics.stats import TradeStatsAggregator
from app.broker.paper import PaperBroker
from app.config.settings import Settings, TradingMode
from app.execution.executor import Executor
from app.models import Candle, Signal, Trade
from app.risk.manager import RiskManager
from app.strategy.base import Strategy
from app.strategy.scalp_smc import SmcScalpStrategy
from app.utils.instruments import get_instrument
from app.utils.logging import get_logger
from app.utils.sessions import SessionFilter

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Immutable summary of a backtest run."""

    trades: list[Trade]
    equity_curve: list[float]
    starting_balance: float
    final_balance: float
    total_pnl: float
    winrate: float
    max_drawdown_pct: float
    total_bars: int
    duration_sec: float
    by_session: dict[str, dict]
    by_symbol: dict[str, dict]

    def summary(self) -> dict[str, object]:
        return {
            "total_trades": len(self.trades),
            "winrate": f"{self.winrate:.2%}",
            "total_pnl": f"{self.total_pnl:.2f}",
            "final_balance": f"{self.final_balance:.2f}",
            "max_drawdown_pct": f"{self.max_drawdown_pct:.2%}",
            "total_bars_processed": self.total_bars,
            "backtest_duration_sec": f"{self.duration_sec:.2f}",
            "by_session": self.by_session,
            "by_symbol": self.by_symbol,
        }


@dataclass(slots=True)
class BacktestEngine:
    """Replay historical candles through the strategy + risk + execution stack.

    Parameters
    ----------
    settings:
        Application configuration (risk params, session hours, etc).
    candles:
        A Polars DataFrame with columns: symbol, timeframe, timestamp, open,
        high, low, close, volume.  Must be sorted by ``timestamp`` ascending.
    strategy:
        Optional custom strategy instance. If ``None``, the default
        ``SmcScalpStrategy`` is instantiated per-symbol.
    spread_pips:
        Constant spread to add to every simulated fill (default 1.0 for FX).
    slippage_pips:
        Constant slippage in pips (default 0.5).
    """

    settings: Settings
    candles: pl.DataFrame
    strategy: Strategy | None = None
    spread_pips: float = 1.0
    slippage_pips: float = 0.5

    _broker: PaperBroker = field(init=False, default_factory=lambda: PaperBroker())
    _strategies: dict[str, Strategy] = field(init=False, default_factory=dict)
    _stats: TradeStatsAggregator | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self._broker = PaperBroker(
            starting_balance=self.settings.account_balance,
            slippage_pips=self.slippage_pips,
        )
        self._stats = TradeStatsAggregator(starting_balance=self.settings.account_balance)

    async def run(self) -> BacktestResult:
        """Execute the backtest. Returns a frozen result."""
        import time

        t0 = time.perf_counter()
        await self._broker.connect()

        risk = RiskManager(
            settings=self.settings,
            starting_balance=self.settings.account_balance,
        )

        closed_trades: list[Trade] = []

        async def _persist(trade: Trade) -> None:
            if trade.status.value.startswith("CLOSED"):
                closed_trades.append(trade)
                assert self._stats is not None
                self._stats.record(trade)

        executor = Executor(
            broker=self._broker,
            risk=risk,
            mode=TradingMode.paper,
            persist_trade=_persist,
        )

        sessions = SessionFilter(
            london_open_utc=self.settings.london_open_utc,
            london_close_utc=self.settings.london_close_utc,
            ny_open_utc=self.settings.ny_open_utc,
            ny_close_utc=self.settings.ny_close_utc,
        )

        # Build per-symbol strategies
        symbols = self.candles["symbol"].unique().to_list()
        for sym in symbols:
            if self.strategy is not None:
                self._strategies[sym] = self.strategy
            else:
                self._strategies[sym] = SmcScalpStrategy(
                    symbol=sym,
                    timeframe=self.settings.primary_timeframe.value,
                    sessions=sessions,
                    target_profit_pct_min=self.settings.target_profit_pct_min,
                    target_profit_pct_max=self.settings.target_profit_pct_max,
                )

        # Main replay loop - iterate row by row in time order
        total_bars = 0
        for row in self.candles.iter_rows(named=True):
            total_bars += 1
            candle = Candle(
                symbol=row["symbol"],
                timeframe=row["timeframe"],
                timestamp=row["timestamp"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row.get("volume", 0.0),
            )

            # Feed quote to paper broker for fills
            inst = get_instrument(candle.symbol)
            half_spread = (self.spread_pips * inst.pip_size) / 2
            bid = candle.close - half_spread
            ask = candle.close + half_spread
            self._broker.set_quote(candle.symbol, bid, ask)

            # Check if any open trades should be closed (SL/TP hit)
            await self._check_sl_tp(executor, candle)

            # Run strategy
            strat = self._strategies.get(candle.symbol)
            if strat is None:
                continue
            signal = await strat.on_candle(candle)
            if signal is not None:
                await executor.handle_signal(signal, spread_pips=self.spread_pips)

        # Force-close any remaining open trades at last known price
        for trade_id, trade in list(executor.open_trades().items()):
            last_bid, last_ask = await self._broker.get_quote(trade.symbol)
            from app.models.signal import SignalDirection
            exit_price = last_bid if trade.direction is SignalDirection.LONG else last_ask
            closed = await executor.close_trade(trade_id, reason="backtest_end", exit_price=exit_price)
            if closed:
                closed_trades.append(closed)
                assert self._stats is not None
                self._stats.record(closed)

        duration = time.perf_counter() - t0
        assert self._stats is not None

        return BacktestResult(
            trades=closed_trades,
            equity_curve=list(self._stats.equity_curve),
            starting_balance=self.settings.account_balance,
            final_balance=self._stats.equity_curve[-1] if self._stats.equity_curve else self.settings.account_balance,
            total_pnl=self._stats.total_pnl,
            winrate=self._stats.winrate,
            max_drawdown_pct=self._stats.max_drawdown,
            total_bars=total_bars,
            duration_sec=duration,
            by_session={k: v.__dict__ for k, v in self._stats.by_session.items()},
            by_symbol={k: v.__dict__ for k, v in self._stats.by_symbol.items()},
        )

    async def _check_sl_tp(self, executor: Executor, candle: Candle) -> None:
        """Simulate SL/TP fills if the current candle's range crosses levels."""
        from app.models.signal import SignalDirection

        for trade_id, trade in list(executor.open_trades().items()):
            if trade.symbol != candle.symbol:
                continue
            if trade.direction is SignalDirection.LONG:
                if candle.low <= trade.stop_loss:
                    await executor.close_trade(trade_id, reason="stop_loss", exit_price=trade.stop_loss)
                elif candle.high >= trade.take_profit:
                    await executor.close_trade(trade_id, reason="take_profit", exit_price=trade.take_profit)
            else:  # SHORT
                if candle.high >= trade.stop_loss:
                    await executor.close_trade(trade_id, reason="stop_loss", exit_price=trade.stop_loss)
                elif candle.low <= trade.take_profit:
                    await executor.close_trade(trade_id, reason="take_profit", exit_price=trade.take_profit)


def load_candles_from_csv(path: str, symbol: str = "EURUSD", timeframe: str = "M1") -> pl.DataFrame:
    """Load a CSV of OHLCV data into the format expected by the backtest engine.

    The CSV should have columns: timestamp (or datetime/date), open, high, low, close, volume.
    """
    df = pl.read_csv(path)
    # Normalize column names
    rename_map: dict[str, str] = {}
    for col in df.columns:
        lower = col.lower().strip()
        if lower in ("datetime", "date", "time", "ts"):
            rename_map[col] = "timestamp"
        elif lower in ("o", "open"):
            rename_map[col] = "open"
        elif lower in ("h", "high"):
            rename_map[col] = "high"
        elif lower in ("l", "low"):
            rename_map[col] = "low"
        elif lower in ("c", "close"):
            rename_map[col] = "close"
        elif lower in ("v", "vol", "volume", "tick_volume"):
            rename_map[col] = "volume"
    df = df.rename(rename_map)

    # Add metadata columns
    df = df.with_columns([
        pl.lit(symbol).alias("symbol"),
        pl.lit(timeframe).alias("timeframe"),
    ])

    # Parse timestamp if string
    if df["timestamp"].dtype == pl.Utf8:
        df = df.with_columns(pl.col("timestamp").str.to_datetime().alias("timestamp"))

    # Ensure volume exists
    if "volume" not in df.columns:
        df = df.with_columns(pl.lit(0.0).alias("volume"))

    return df.sort("timestamp")
