"""Backtesting script.

Replays historical candle data through the strategy + risk + execution
pipeline and prints performance statistics.

Usage:
    python -m scripts.run_backtest --csv data/EURUSD_M1.csv --symbol EURUSD
    python -m scripts.run_backtest --csv data/XAUUSD_M1.csv --symbol XAUUSD --spread 3.0

The CSV should contain columns: timestamp (or datetime), open, high, low, close, volume.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import typer

from app.analytics.backtest import BacktestEngine, load_candles_from_csv
from app.config import get_settings
from app.utils.logging import configure_logging, get_logger

app_cli = typer.Typer(name="backtest", help="Run a historical backtest.")


@app_cli.command()
def run(
    csv: str = typer.Option(..., help="Path to OHLCV CSV file"),
    symbol: str = typer.Option("EURUSD", help="Symbol name"),
    timeframe: str = typer.Option("M1", help="Timeframe label"),
    spread: float = typer.Option(1.0, help="Simulated spread in pips"),
    slippage: float = typer.Option(0.5, help="Simulated slippage in pips"),
    output: str = typer.Option("", help="Path to write JSON results (optional)"),
) -> None:
    """Execute a backtest against a CSV of historical candles."""
    settings = get_settings()
    configure_logging(level="INFO", json_output=False)
    log = get_logger(__name__)

    csv_path = Path(csv)
    if not csv_path.exists():
        log.error("CSV file not found: %s", csv)
        sys.exit(1)

    log.info("loading candles from %s", csv_path)
    candles = load_candles_from_csv(str(csv_path), symbol=symbol, timeframe=timeframe)
    log.info("loaded %d bars for %s/%s", len(candles), symbol, timeframe)

    engine = BacktestEngine(
        settings=settings,
        candles=candles,
        spread_pips=spread,
        slippage_pips=slippage,
    )

    log.info("running backtest...")
    t0 = time.perf_counter()
    result = asyncio.run(engine.run())
    elapsed = time.perf_counter() - t0

    # Print summary
    summary = result.summary()
    log.info("=" * 60)
    log.info("BACKTEST RESULTS")
    log.info("=" * 60)
    for key, val in summary.items():
        if isinstance(val, dict):
            log.info("  %s:", key)
            for k2, v2 in val.items():
                log.info("    %s: %s", k2, v2)
        else:
            log.info("  %s: %s", key, val)
    log.info("=" * 60)
    log.info("Wall time: %.2fs | Bars/sec: %.0f", elapsed, result.total_bars / max(elapsed, 0.01))

    # Optionally write JSON
    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w") as f:
            json.dump(summary, f, indent=2, default=str)
        log.info("results written to %s", out_path)


def main() -> None:
    """Entrypoint for pyproject.toml scripts."""
    app_cli()


if __name__ == "__main__":
    main()
