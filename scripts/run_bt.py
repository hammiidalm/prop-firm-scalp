"""Backtesting via kernc/backtesting.py — compatible wrapper for SmcScalpStrategy.

Solves the async/sync mismatch by pre-computing all signals ONCE with asyncio,
then feeding them into the synchronous backtesting.py Strategy.next() loop.

Usage:
    python -m scripts.run_bt --csv data/EURUSD_M1.csv --symbol EURUSD
    python -m scripts.run_bt --csv data/EURUSD_M1.csv --symbol EURUSD --spread 1.0 --cash 100000

Requirements:
    pip install backtesting polars

Results should match scripts/run_backtest.py (native runner) within ±5% winrate / ±10% PF.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl
import typer

# backtesting.py imports
from backtesting import Backtest, Strategy
from backtesting.lib import crossover

from app.config import get_settings
from app.models import Candle, Signal
from app.models.signal import SignalDirection
from app.strategy.scalp_smc import SmcScalpStrategy
from app.utils.instruments import get_instrument
from app.utils.logging import configure_logging, get_logger
from app.utils.sessions import SessionFilter

log = get_logger(__name__)

app_cli = typer.Typer(name="run_bt", help="Backtest SmcScalp via backtesting.py (kernc).")


# ===========================================================================
# Signal precomputation (runs async ONCE before backtest)
# ===========================================================================


@dataclass(frozen=True)
class PrecomputedSignal:
    """A signal tagged with its bar index for lookup during backtest."""

    bar_index: int
    direction: str  # "LONG" or "SHORT"
    entry_price: float
    stop_loss: float
    take_profit: float
    confidence: float


async def precompute_signals(
    candles: list[Candle],
    symbol: str,
    timeframe: str,
    settings: Any,
) -> list[PrecomputedSignal]:
    """Run SmcScalpStrategy over all candles and collect signals.

    This is called ONCE before the backtesting.py Backtest starts.
    No async happens inside the backtest loop itself.
    """
    sessions = SessionFilter(
        london_open_utc=settings.london_open_utc,
        london_close_utc=settings.london_close_utc,
        ny_open_utc=settings.ny_open_utc,
        ny_close_utc=settings.ny_close_utc,
    )

    strategy = SmcScalpStrategy(
        symbol=symbol,
        timeframe=timeframe,
        sessions=sessions,
        target_profit_pct_min=settings.target_profit_pct_min,
        target_profit_pct_max=settings.target_profit_pct_max,
    )

    signals: list[PrecomputedSignal] = []

    for idx, candle in enumerate(candles):
        signal = await strategy.on_candle(candle)
        if signal is not None:
            signals.append(PrecomputedSignal(
                bar_index=idx,
                direction=signal.direction.value,
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                confidence=signal.confidence,
            ))

    return signals


# ===========================================================================
# backtesting.py Strategy wrapper
# ===========================================================================


# Global signal lookup populated before Backtest.run()
_SIGNAL_MAP: dict[int, PrecomputedSignal] = {}


class SmcBt(Strategy):
    """backtesting.py Strategy that looks up pre-computed SMC signals.

    No async, no per-bar strategy evaluation — just a dict lookup.
    SL and TP are set exactly as the native runner would.
    """

    # Parameters (can be optimized via bt.optimize())
    spread_pips: float = 1.0

    def init(self):
        """Nothing to compute — signals are pre-loaded in _SIGNAL_MAP."""
        pass

    def next(self):
        """Check if there's a signal for the current bar index."""
        idx = len(self.data) - 1  # current bar index in the full dataset
        signal = _SIGNAL_MAP.get(idx)

        if signal is None:
            return

        # Don't open if already in a position
        if self.position:
            return

        # Calculate pip size for SL/TP
        instrument = get_instrument(self.data.df.attrs.get("symbol", "EURUSD"))
        half_spread = self.spread_pips * instrument.pip_size / 2

        if signal.direction == "LONG":
            entry = signal.entry_price + half_spread  # buy at ask
            sl = signal.stop_loss
            tp = signal.take_profit
            self.buy(sl=sl, tp=tp)

        elif signal.direction == "SHORT":
            entry = signal.entry_price - half_spread  # sell at bid
            sl = signal.stop_loss
            tp = signal.take_profit
            self.sell(sl=sl, tp=tp)


# ===========================================================================
# Data loading
# ===========================================================================


def load_ohlcv_for_bt(csv_path: str, symbol: str, timeframe: str) -> tuple[Any, list[Candle]]:
    """Load CSV into both a pandas DataFrame (for backtesting.py) and Candle list.

    Returns:
        (pandas_df, candle_list)
    """
    import pandas as pd

    # Read with polars for consistency with the native runner
    df_pl = pl.read_csv(csv_path)

    # Normalize columns
    rename_map: dict[str, str] = {}
    for col in df_pl.columns:
        lower = col.lower().strip()
        if lower in ("datetime", "date", "time", "ts", "timestamp"):
            rename_map[col] = "timestamp"
        elif lower in ("o", "open"):
            rename_map[col] = "Open"
        elif lower in ("h", "high"):
            rename_map[col] = "High"
        elif lower in ("l", "low"):
            rename_map[col] = "Low"
        elif lower in ("c", "close"):
            rename_map[col] = "Close"
        elif lower in ("v", "vol", "volume", "tick_volume"):
            rename_map[col] = "Volume"
    df_pl = df_pl.rename(rename_map)

    # Convert to pandas for backtesting.py
    df_pd = df_pl.to_pandas()
    df_pd["timestamp"] = pd.to_datetime(df_pd["timestamp"], utc=True)
    df_pd = df_pd.set_index("timestamp").sort_index()

    # Ensure required columns exist
    for col in ("Open", "High", "Low", "Close"):
        if col not in df_pd.columns:
            raise ValueError(f"Missing column: {col}")
    if "Volume" not in df_pd.columns:
        df_pd["Volume"] = 0

    # Store symbol in attrs for strategy access
    df_pd.attrs["symbol"] = symbol

    # Build Candle list for signal precomputation
    candles: list[Candle] = []
    for ts, row in df_pd.iterrows():
        candles.append(Candle(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=ts.to_pydatetime(),
            open=float(row["Open"]),
            high=float(row["High"]),
            low=float(row["Low"]),
            close=float(row["Close"]),
            volume=float(row.get("Volume", 0)),
        ))

    return df_pd, candles


# ===========================================================================
# CLI
# ===========================================================================


@app_cli.command()
def run(
    csv: str = typer.Option(..., help="Path to OHLCV CSV file"),
    symbol: str = typer.Option("EURUSD", help="Symbol name"),
    timeframe: str = typer.Option("M1", help="Timeframe (M1, M5, etc)"),
    spread: float = typer.Option(1.0, help="Spread in pips"),
    cash: float = typer.Option(100_000.0, help="Starting cash"),
    commission: float = typer.Option(0.0, help="Commission per trade (fraction)"),
) -> None:
    """Run SmcScalpStrategy via backtesting.py with pre-computed signals."""
    global _SIGNAL_MAP

    configure_logging(level="INFO", json_output=False)
    settings = get_settings()

    csv_path = Path(csv)
    if not csv_path.exists():
        log.error("CSV not found: %s", csv_path)
        sys.exit(1)

    # 1. Load data
    log.info("Loading candles from %s...", csv_path)
    df_pd, candles = load_ohlcv_for_bt(str(csv_path), symbol=symbol, timeframe=timeframe)
    log.info("Loaded %d bars for %s/%s", len(candles), symbol, timeframe)

    # 2. Pre-compute signals (async, runs ONCE)
    log.info("Pre-computing signals via SmcScalpStrategy (async, one-shot)...")
    signals = asyncio.run(precompute_signals(candles, symbol, timeframe, settings))
    log.info("Found %d signals in %d bars (%.1f%% hit rate)",
             len(signals), len(candles), len(signals) / max(len(candles), 1) * 100)

    # 3. Build signal lookup map (bar_index → signal)
    _SIGNAL_MAP = {s.bar_index: s for s in signals}

    # 4. Run backtesting.py
    log.info("Running backtesting.py Backtest...")
    bt = Backtest(
        df_pd,
        SmcBt,
        cash=cash,
        commission=commission,
        exclusive_orders=True,  # one position at a time
        trade_on_close=True,
    )

    stats = bt.run(spread_pips=spread)

    # 5. Print results
    print("\n" + "=" * 60)
    print("  BACKTEST RESULTS (backtesting.py + SmcScalp)")
    print("=" * 60)
    print(stats)
    print("=" * 60)

    # Compare key metrics
    native_style = {
        "Total Trades": stats["# Trades"],
        "Win Rate": f"{stats['Win Rate [%]']:.2f}%",
        "Profit Factor": f"{stats.get('Profit Factor', 0):.2f}",
        "Net P&L": f"${stats['Equity Final [$]'] - cash:.2f}",
        "Max Drawdown": f"{stats['Max. Drawdown [%]']:.2f}%",
        "Return": f"{stats['Return [%]']:.2f}%",
    }
    print("\nKey Metrics (for comparison with native runner):")
    for k, v in native_style.items():
        print(f"  {k}: {v}")
    print()


def main() -> None:
    app_cli()


if __name__ == "__main__":
    main()
