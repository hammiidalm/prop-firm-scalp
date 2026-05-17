"""Backtest runner for SmcScalpStrategy.

Usage:
    python -m app.backtesting.runner \\
        --symbol EURUSD \\
        --timeframe M5 \\
        --data data/sample_eurusd_m5.csv \\
        --min-confluence 60

Output: JSON report + human-readable summary to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import polars as pl
except ImportError:
    pl = None  # type: ignore[assignment]

from app.strategy.scalp_smc import SmcScalpStrategy
from app.models.candle import Candle
from app.utils.sessions import SessionFilter


# ============================================================
# Types
# ============================================================

@dataclass(slots=True)
class Trade:
    """Closed or open trade from backtest simulation."""
    entry_index: int
    entry_time: datetime
    entry_price: float
    direction: str  # "LONG" or "SHORT"
    sl: float
    tp: float
    exit_index: int | None = None
    exit_time: datetime | None = None
    exit_price: float | None = None
    exit_reason: str = ""  # "TP", "SL", "END"
    pnl: float = 0.0
    rr_ratio: float = 0.0
    signal_confidence: float = 0.0
    signal_tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BacktestResult:
    """Final backtest report."""
    symbol: str
    timeframe: str
    min_confluence: int
    total_bars: int
    signals_generated: int
    trades_executed: int
    wins: int
    losses: int
    win_rate: float
    net_pnl: float
    gross_profit: float
    gross_loss: float
    profit_factor: float
    max_drawdown: float
    avg_rr: float
    avg_pnl_per_trade: float
    trades: list[Trade] = field(default_factory=list)


# ============================================================
# Candle loader
# ============================================================

def load_candles_csv(path: str | Path, symbol: str = "EURUSD", tf: str = "M5") -> list[Candle]:
    """Load candles from CSV using Polars (fast) or csv module fallback."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    if pl is not None:
        df = pl.read_csv(
            path,
            try_parse_dates=True,
            schema={
                "timestamp": pl.String,
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Float64,
            },
        )
        # Convert timestamp column to UTC datetime
        df = df.with_columns(
            pl.col("timestamp").str.to_datetime(time_zone="UTC")
        )
        candles: list[Candle] = []
        for row in df.iter_rows(named=True):
            ts: datetime = row["timestamp"]
            # Polars returns timezone-aware → make naive UTC compatible
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            candles.append(
                Candle(
                    symbol=symbol,
                    timeframe=tf,
                    timestamp=ts,
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    volume=row.get("volume", 0.0),
                )
            )
        return candles

    # Fallback: manual CSV (no polars)
    import csv
    import io
    candles = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        required = {"timestamp", "open", "high", "low", "close"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError(f"CSV must contain: {required}, got {reader.fieldnames}")
        for row in reader:
            ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            if ts.tzinfo:
                ts = ts.replace(tzinfo=None)
            candles.append(
                Candle(
                    symbol=symbol,
                    timeframe=tf,
                    timestamp=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0.0)),
                )
            )
    return candles


# ============================================================
# Simulation
# ============================================================

def _simulate_close_old_removed(): pass
# _simulate_close replaced by inline exit loop in run_backtest for trailing SL + target lock


def run_backtest(
    candles: list[Candle],
    symbol: str = "EURUSD",
    tf: str = "M5",
    min_confluence: int = 60,
) -> BacktestResult:
    """Run backtest on candle list using SmcScalpStrategy."""
    if not candles:
        raise ValueError("Empty candle list")

    # Initialize strategy
    _sf = SessionFilter(london_open_utc=7, london_close_utc=11, ny_open_utc=14, ny_close_utc=17)
    strategy = SmcScalpStrategy(
        symbol=symbol,
        timeframe=tf,
        sessions=_sf,
    )
    strategy.min_confluence = min_confluence

    trades: list[Trade] = []
    open_trade: Trade | None = None
    signals_generated = 0
    last_entry_idx = -9999
    cooldown_bars = 3  # avoid overtrading

    for idx, candle in enumerate(candles):
        if open_trade is not None:
            ep = open_trade.entry_price
            dirn = open_trade.direction
            c_high = candle.high
            c_low = candle.low
            if dirn == "LONG":
                cur_profit_pct = (candle.close - ep) / ep
                if cur_profit_pct >= 0.001:
                    open_trade.exit_index = idx
                    open_trade.exit_time = candle.timestamp
                    open_trade.exit_price = ep * 1.001
                    open_trade.exit_reason = "TARGET_0.1%"
                    open_trade.pnl = open_trade.exit_price - ep
                    trades.append(open_trade)
                    open_trade = None
                elif cur_profit_pct >= 0.0005:
                    open_trade.sl = max(open_trade.sl, ep)
                if open_trade is not None and c_low <= open_trade.sl:
                    open_trade.exit_index = idx
                    open_trade.exit_time = candle.timestamp
                    open_trade.exit_price = open_trade.sl
                    open_trade.exit_reason = "SL"
                    open_trade.pnl = open_trade.sl - ep
                    trades.append(open_trade)
                    open_trade = None
            else:
                cur_profit_pct = (ep - candle.close) / ep
                if cur_profit_pct >= 0.001:
                    open_trade.exit_index = idx
                    open_trade.exit_time = candle.timestamp
                    open_trade.exit_price = ep * 0.999
                    open_trade.exit_reason = "TARGET_0.1%"
                    open_trade.pnl = ep - open_trade.exit_price
                    trades.append(open_trade)
                    open_trade = None
                elif cur_profit_pct >= 0.0005:
                    open_trade.sl = min(open_trade.sl, ep)
                if open_trade is not None and c_high >= open_trade.sl:
                    open_trade.exit_index = idx
                    open_trade.exit_time = candle.timestamp
                    open_trade.exit_price = open_trade.sl
                    open_trade.exit_reason = "SL"
                    open_trade.pnl = ep - open_trade.sl
                    trades.append(open_trade)
                    open_trade = None

        in_cooldown = idx - last_entry_idx < cooldown_bars
        if open_trade is None and not in_cooldown:
            try:
                import asyncio
                loop = asyncio.new_event_loop()
                signal = loop.run_until_complete(strategy.on_candle(candle))
                loop.close()
            except Exception as exc:
                print(f"Strategy err {idx}: {exc}", file=sys.stderr)
                signal = None

            if signal is not None and signal.confidence >= float(min_confluence)/100:
                signals_generated += 1
                last_entry_idx = idx
                open_trade = Trade(
                    entry_index=idx,
                    entry_time=candle.timestamp,
                    entry_price=float(signal.entry_price),
                    direction=str(signal.direction.value),
                    sl=float(signal.stop_loss),
                    tp=float(signal.take_profit),
                    signal_confidence=signal.confidence,
                    signal_tags=list(getattr(signal, "structure_tags", [])),
                )

    if open_trade is not None:
        c_last = candles[-1]
        open_trade.exit_index = len(candles) - 1
        open_trade.exit_time = c_last.timestamp
        open_trade.exit_price = c_last.close
        open_trade.exit_reason = "END_DATA"
        ep = open_trade.entry_price
        if open_trade.direction == "LONG":
            open_trade.pnl = open_trade.exit_price - ep
        else:
            open_trade.pnl = ep - open_trade.exit_price
        trades.append(open_trade)

    # Build metrics
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    pnls = [t.pnl for t in trades]

    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p <= 0))
    net_pnl = sum(pnls)

    win_rate = len(wins) / len(trades) if trades else 0.0
    # avg_rr using actual SL at exit (trailing SL may have moved)
    def _rr(t):
        risk = abs(t.entry_price - t.sl)
        if risk == 0:
            return 0.0
        return abs(t.pnl) / risk
    avg_rr = sum(_rr(t) for t in trades) / len(trades) if trades else 0.0
    avg_pnl = net_pnl / len(trades) if trades else 0.0

    # Max drawdown
    equity_curve: list[float] = [0.0]
    peak = 0.0
    max_dd = 0.0
    for trade in trades:
        equity_curve.append(equity_curve[-1] + trade.pnl)
    for val in equity_curve:
        if val > peak:
            peak = val
        dd = peak - val
        if dd > max_dd:
            max_dd = dd

    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0

    return BacktestResult(
        symbol=symbol,
        timeframe=tf,
        min_confluence=min_confluence,
        total_bars=len(candles),
        signals_generated=signals_generated,
        trades_executed=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=round(win_rate, 4),
        net_pnl=round(net_pnl, 4),
        gross_profit=round(gross_profit, 4),
        gross_loss=round(gross_loss, 4),
        profit_factor=round(profit_factor, 4) if profit_factor != float("inf") else 9999.0,
        max_drawdown=round(max_dd, 4),
        avg_rr=round(avg_rr, 4),
        avg_pnl_per_trade=round(avg_pnl, 4),
        trades=trades,
    )


# ============================================================
# CLI
# ============================================================

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run SMC backtest on OHLCV CSV data.")
    parser.add_argument("--data", type=str, required=True, help="Path to OHLCV CSV file")
    parser.add_argument("--symbol", type=str, default="EURUSD", help="Trading symbol")
    parser.add_argument("--timeframe", type=str, default="M5", help="Candle timeframe")
    parser.add_argument("--min-confluence", type=int, default=60, help="Minimum confluence threshold")
    parser.add_argument("--output", type=str, default=None, help="Save JSON report to file")
    return parser.parse_args(argv)


def _print_report(result: BacktestResult) -> None:
    """Print human-readable summary."""
    sep = "=" * 60
    print(sep)
    print(f"  SM BACKTEST REPORT  —  {result.symbol}  {result.timeframe}")
    print(sep)
    print(f"  Total bars            : {result.total_bars}")
    print(f"  Signals generated     : {result.signals_generated}")
    print(f"  Trades executed       : {result.trades_executed}")
    print(f"  Wins / Losses         : {result.wins} / {result.losses}")
    print(f"  Win rate              : {result.win_rate:.2%}")
    print(f"  Net PnL               : {result.net_pnl:.4f}")
    print(f"  Gross profit          : {result.gross_profit:.4f}")
    print(f"  Gross loss            : {result.gross_loss:.4f}")
    print(f"  Profit factor         : {result.profit_factor:.2f}")
    print(f"  Max drawdown          : {result.max_drawdown:.4f}")
    print(f"  Avg R:R               : {result.avg_rr:.2f}")
    print(f"  Avg PnL/trade         : {result.avg_pnl_per_trade:.4f}")
    print(sep)
    print(f"  Trade list:")
    for t in result.trades:
        pnl_fmt = f"({t.pnl:.4f})" if t.pnl < 0 else f"+{t.pnl:.4f}"
        print(f"  [{t.entry_time.isoformat()} → {t.exit_time.isoformat() if t.exit_time else '?'}] "
              f"{t.direction} @{t.entry_price:.4f} SL={t.sl:.4f} TP={t.tp:.4f} "
              f"exit={t.exit_reason} PnL={pnl_fmt}")
    print(sep)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    candles = load_candles_csv(args.data, args.symbol, args.timeframe)
    result = run_backtest(candles, args.symbol, args.timeframe, args.min_confluence)
    _print_report(result)

    if args.output:
        out_path = Path(args.output)
        payload = {
            **{k: v for k, v in result.__dict__.items() if k != "trades"},
            "trades": [
                {**t.__dict__, "entry_time": t.entry_time.isoformat(), "exit_time": t.exit_time.isoformat() if t.exit_time else None}
                for t in result.trades
            ],
        }
        out_path.write_text(json.dumps(payload, indent=2))
        print(f"\nReport saved to {out_path}")


if __name__ == "__main__":
    main()
