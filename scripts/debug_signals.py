"""Quick debug: run SmcScalpStrategy on synthetic data and print signals."""
import asyncio
import datetime
import random
from pathlib import Path

from app.models.candle import Candle
from app.strategy.scalp_smc import SmcScalpStrategy
from app.utils.instruments import get_instrument
from app.utils.sessions import SessionFilter

random.seed(7)

# Build synthetic candles
rows: list[Candle] = []
price = 1.08550
base = datetime.datetime(2025, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)
for i in range(200):
    ts = base + datetime.timedelta(minutes=5 * i)
    trend = 0.00007 if (i % 50 < 25) else -0.00007
    noise = random.gauss(0, 0.00055)
    if i == 50:
        noise -= 0.0025     # sweep low
    if i == 120:
        noise += 0.0025    # sweep high
    op = price
    cl = price + trend + noise
    hi = max(op, cl) + abs(random.gauss(0, 0.00070))
    lo = min(op, cl) - abs(random.gauss(0, 0.00070))
    rows.append(
        Candle(
            symbol="EURUSD",
            timeframe="M5",
            timestamp=ts,
            open=op,
            high=hi,
            low=lo,
            close=cl,
            volume=0.0,
        )
    )
    price = cl

# Init strategy
_sf = SessionFilter(london_open_utc=7, london_close_utc=11, ny_open_utc=14, ny_close_utc=17)
strategy = SmcScalpStrategy(symbol="EURUSD", timeframe="M5", sessions=_sf)
strategy.min_confluence = 60
strategy.min_rr = 1.2
_instr = get_instrument("EURUSD")
print(f"Instrument delta: {_instr.price_delta(1.0) if _instr else 'N/A'}")

sigs = 0
for i, c in enumerate(rows):
    sym = _instr.symbol if _instr else "EURUSD"
    if not _sf.is_active(c.timestamp, sym):
        continue
    try:
        loop = asyncio.new_event_loop()
        sig = loop.run_until_complete(strategy.on_candle(c))
        loop.close()
        if sig:
            sigs += 1
            rr = float(sig.rr_ratio)
            tags = getattr(sig, "structure_tags", [])
            print(
                f"bar {i:3}: {sig.direction.value} @{sig.entry_price:.5f} "
                f"SL={sig.stop_loss:.5f} TP={sig.take_profit:.5f} "
                f"conf={sig.confidence:.2f} rr={rr:.2f} tags={tags[-2:]}"
            )
    except Exception as e:
        print(f"err {i}: {e}")

print(f"\nTotal signals: {sigs}")
