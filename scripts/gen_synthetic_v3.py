import random
import csv
import datetime
from pathlib import Path

random.seed(2025)
out = Path("/home/ubuntu/prop-firm-scalp/data/synthetic_eurusd_m5_v3.csv")
out.parent.mkdir(exist_ok=True)
price = 1.08500
rows = []
base = datetime.datetime(2025, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)

for i in range(5000):
    ts = base + datetime.timedelta(minutes=5 * i)
    trend = 0.00005 if (i % 300) < 150 else -0.00005
    noise = random.gauss(0, 0.00030)
    # inject 15-30 pip sweep every 500 bars
    if i % 500 == 0:
        if i % 1000 < 500:
            noise -= 0.0015
        else:
            noise += 0.0015
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
