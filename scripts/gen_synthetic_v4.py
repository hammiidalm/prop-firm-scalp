import random, csv, datetime
from pathlib import Path

random.seed(2026)
out = Path("/home/ubuntu/prop-firm-scalp/data/synthetic_eurusd_m5_v4.csv")
out.parent.mkdir(exist_ok=True)

price = 1.08500
rows = []
base = datetime.datetime(2025, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)

# build with periodic trend reversals and explicit sweeps every ~250 bars
for i in range(5000):
    ts = base + datetime.timedelta(minutes=5 * i)

    # slowly alternating trend segment
    seg = i // 250
    trend = 0.00008 if seg % 2 == 0 else -0.00008

    # noise: ~4-8 pip average
    noise = random.gauss(0, 0.00055)

    # inject liquidity sweeps every 250 bars (±15 pip spike)
    phase = i % 250
    if phase == 0:
        noise += 0.0025   # sweep high
    if phase == 125:
        noise -= 0.0025   # sweep low

    open_p = price
    close_p = price + trend + noise
    hi = max(open_p, close_p) + abs(random.gauss(0, 0.00040))
    lo = min(open_p, close_p) - abs(random.gauss(0, 0.00040))
    rows.append((
        ts.isoformat(),
        round(open_p, 5),
        round(hi, 5),
        round(lo, 5),
        round(close_p, 5),
        random.uniform(0, 100)
    ))
    price = close_p

with out.open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
    w.writerows(rows)

print(f"Generated {len(rows)} rows -> {out}")
