"""Generate synthetic OHLCV data for testing/backtesting.

Creates a CSV with realistic price action patterns (random walk with
mean-reversion) for development and CI testing.

Usage:
    python -m scripts.generate_sample_data --symbol EURUSD --bars 5000 --output data/EURUSD_M1.csv
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path

import typer

app_cli = typer.Typer(name="generate-data", help="Generate synthetic OHLCV test data.")


@app_cli.command()
def generate(
    symbol: str = typer.Option("EURUSD", help="Symbol name"),
    bars: int = typer.Option(5000, help="Number of bars to generate"),
    timeframe_minutes: int = typer.Option(1, help="Bar duration in minutes"),
    output: str = typer.Option("data/sample_EURUSD_M1.csv", help="Output CSV path"),
    seed: int = typer.Option(42, help="Random seed for reproducibility"),
) -> None:
    """Generate a synthetic OHLCV CSV file."""
    random.seed(seed)
    Path(output).parent.mkdir(parents=True, exist_ok=True)

    # Starting conditions per symbol
    start_prices = {"EURUSD": 1.0850, "XAUUSD": 2350.0, "GBPUSD": 1.2650}
    pip_sizes = {"EURUSD": 0.0001, "XAUUSD": 0.10, "GBPUSD": 0.0001}

    price = start_prices.get(symbol, 1.0)
    pip = pip_sizes.get(symbol, 0.0001)
    volatility = 8.0 * pip  # ~8 pips per bar typical range

    ts = datetime(2024, 1, 2, 7, 0, 0)  # Start in London session
    delta = timedelta(minutes=timeframe_minutes)

    lines = ["timestamp,open,high,low,close,volume\n"]
    for _ in range(bars):
        # Skip weekend hours (simplified: skip Sat/Sun)
        while ts.weekday() >= 5:
            ts += timedelta(days=1)
            ts = ts.replace(hour=7, minute=0, second=0)

        o = price
        # Random walk with slight mean-reversion
        move = random.gauss(0, volatility)
        c = o + move

        # High/low extend beyond open/close
        h = max(o, c) + abs(random.gauss(0, volatility * 0.5))
        l = min(o, c) - abs(random.gauss(0, volatility * 0.5))  # noqa: E741

        # Ensure valid OHLC
        h = max(h, o, c)
        l = min(l, o, c)  # noqa: E741

        vol = random.randint(50, 500)
        lines.append(f"{ts.isoformat()},{o:.5f},{h:.5f},{l:.5f},{c:.5f},{vol}\n")

        price = c
        ts += delta

    with open(output, "w") as f:
        f.writelines(lines)

    print(f"Generated {bars} bars -> {output}")


def main() -> None:
    app_cli()


if __name__ == "__main__":
    main()
