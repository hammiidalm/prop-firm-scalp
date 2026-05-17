import random, csv, datetime
from pathlib import Path

random.seed(2027)
out = Path("/home/ubuntu/prop-firm-scalp/data/synthetic_eurusd_m5_v5.csv")
out.parent.mkdir(exist_ok=True)

price = 1.08500
rows = []
base = datetime.datetime(2025, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)

# swing every 250 bars, heavy sweeps
for i in range(5000):
    ts = base + datetime.timedelta(minutes=5 * i)
    seg = i // 250
    trend = 0.00018 if seg % 2 == 0 else -0.00018    # ~18 pip every bar over trend
    noise = random.gauss(0, 0.00120)                  # ~12 pip average wick
    phase = i % 250
    if phase == 0:
        noise += 0.0050    # 50 pip sweep hi
    if phase == 125:
        noise -= 0.0050    # 50 pip sweep lo
    op = price
    cl = price + trend + noise
    hi = max(op, cl) + abs(random.gauss(0, 0.00080))
    lo = min(op, cl) - abs(random.gauss(0, 0.00080))
    rows.append((
        ts.isoformat(),
        round(op, 5), round(hi, 5), round(lo, 5), round(cl, 5),
        random.uniform(0, 100)
    ))
    price = cl

with out.open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
    w.writerows(rows)
print(f"Generated {len(rows)} rows -> {out}")
