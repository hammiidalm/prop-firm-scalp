# prop-firm-scalp

Production-grade async scalping bot for **prop-firm forex trading** via the [TradeLocker](https://tradelocker.com) API.

Built for disciplined traders who prioritize **capital preservation**, **low drawdown**, and **precision entries** over aggressive returns.

---

## Key Features

| Category | Details |
|----------|---------|
| **Strategy** | SMC v2: HTF Bias filter, Order Blocks, FVG, liquidity sweeps, BOS/CHOCH, rejection candles, dynamic confluence scoring (0–100) |
| **Risk** | 0.25-0.5% per trade, 1% daily DD cap, 3-loss circuit breaker, 5 trades/day max |
| **Broker** | TradeLocker REST + WebSocket, JWT auto-refresh, order retry with backoff |
| **Symbols** | EURUSD and XAUUSD optimized (extensible to any TradeLocker symbol) |
| **Modes** | Paper trading, Semi-auto (Telegram confirmation), Full-auto |
| **Sessions** | London (07-11 UTC) and New York (12-16 UTC) only |
| **Infra** | Docker, systemd, PostgreSQL/SQLite, optional Redis, FastAPI dashboard |
| **Alerts** | Telegram (MarkdownV2 with confluence display) + Discord webhooks |
| **Analytics** | Backtesting engine, equity curve, winrate, session stats, rejection analysis |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Engine (Orchestrator)                      │
├─────────┬───────────┬──────────┬──────────┬──────────┬──────────┤
│ Strategy│   Risk    │Execution │  Broker  │  Journal │  Notify  │
│ (SMC)   │  Manager  │ Executor │ (TL/Paper)│  (SQL)  │ (TG/DC)  │
├─────────┴───────────┴──────────┴──────────┴──────────┴──────────┤
│               WebSocket Client (auto-reconnect)                   │
├─────────────────────────────────────────────────────────────────┤
│              FastAPI Dashboard (/health, /api/v1/*)               │
└─────────────────────────────────────────────────────────────────┘
```

### Directory Layout

```
prop-firm-scalp/
├── app/
│   ├── api/              # FastAPI dashboard + healthcheck
│   ├── analytics/        # Backtesting engine + stats aggregator
│   ├── broker/           # TradeLocker client + paper broker
│   ├── config/           # Pydantic settings from .env
│   ├── engine/           # Main orchestrator
│   ├── execution/        # Signal → Order translation
│   ├── journal/          # SQLAlchemy trade persistence
│   ├── models/           # Domain models (Candle, Signal, Order, Trade)
│   ├── notifications/    # Telegram + Discord dispatchers
│   ├── risk/             # Position sizing + DD guards
│   ├── strategy/         # Market structure + SMC scalp strategy
│   ├── utils/            # Logging, sessions, time, instruments
│   └── websocket/        # Resilient WS client with auto-reconnect
├── scripts/              # CLI entrypoints (live, backtest, data gen)
├── tests/                # pytest unit tests
├── docker/               # systemd unit, compose overrides
├── Dockerfile            # Multi-stage production image
├── docker-compose.yml    # Full stack (bot + postgres + redis)
├── pyproject.toml        # Project metadata + tool config
├── requirements.txt      # Runtime dependencies
├── requirements-dev.txt  # Dev/test dependencies
└── .env.example          # Template for secrets
```

---

## Quick Start

### Prerequisites

- Python 3.11+
- Docker & Docker Compose (for containerized deployment)
- A TradeLocker demo or live account

### 1. Clone & Configure

```bash
git clone https://github.com/hammiidalm/prop-firm-scalp.git
cd prop-firm-scalp
cp .env.example .env
# Edit .env with your TradeLocker credentials and preferences
```

### 2. Run in Paper Mode (no broker needed)

```bash
# Install dependencies
pip install -e ".[dev]"

# Generate sample data
python -m scripts.generate_sample_data --symbol EURUSD --bars 10000

# Run backtest
python -m scripts.run_backtest --csv data/sample_EURUSD_M1.csv --symbol EURUSD

# Run live paper trading
APP_MODE=paper python -m scripts.run_live
```

### 3. Docker Deployment

```bash
# Paper mode (SQLite, no external deps)
docker compose -f docker-compose.yml -f docker/docker-compose.paper.yml up -d

# Full production (Postgres + bot)
docker compose up -d

# View logs
docker compose logs -f bot
```

### 4. systemd Service (Oracle Linux / RHEL)

```bash
sudo cp docker/prop-firm-scalp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now prop-firm-scalp
```

---

## Configuration

All configuration is via environment variables (see `.env.example`).

### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_MODE` | `paper` | `paper`, `semi_auto`, or `full_auto` |
| `SYMBOLS` | `EURUSD,XAUUSD` | Comma-separated trading symbols |
| `RISK_PER_TRADE_PCT` | `0.0035` | Risk per trade as decimal (0.35%) |
| `MAX_DAILY_LOSS_PCT` | `0.01` | Daily drawdown circuit breaker (1%) |
| `MAX_TRADES_PER_DAY` | `5` | Hard daily trade limit |
| `MAX_CONSECUTIVE_LOSSES` | `3` | Stop trading after N losses in a row |
| `MAX_SPREAD_PIPS_FX` | `1.5` | Max acceptable spread for FX pairs |
| `MAX_SPREAD_PIPS_METALS` | `35` | Max acceptable spread for metals |

### TradeLocker Credentials

| Variable | Description |
|----------|-------------|
| `TL_BASE_URL` | REST API base URL |
| `TL_WS_URL` | WebSocket URL |
| `TL_EMAIL` | Account email |
| `TL_PASSWORD` | Account password |
| `TL_SERVER` | Broker server name |
| `TL_ACCOUNT_ID` | Trading account ID |
| `TL_ACCOUNT_NUM` | Trading account number |

---

## Trading Modes

### Paper Mode
Zero-risk simulation using the built-in paper broker. No real orders are placed. Ideal for strategy development and backtesting.

### Semi-Auto Mode
1. Bot detects a valid setup (sweep + rejection + BOS confirmation)
2. Sends a formatted signal to Telegram with entry/SL/TP/RR/lots
3. Trader reviews and replies `/confirm <id>` to execute
4. Signal expires after 90 seconds if unconfirmed

### Full-Auto Mode
Signals approved by the risk manager are sent directly to TradeLocker. The bot manages the entire trade lifecycle autonomously.

---

## Strategy: SMC Scalp v2 — Confluence-Scored

The strategy implements institutional **Smart Money Concepts** with a multi-timeframe,
confluence-based approach. A signal is only generated when sufficient evidence aligns —
no single-factor entries.

### Core Concepts

| Concept | Description |
|---------|-------------|
| **Higher-Timeframe Bias** | A separate `HTFStructure` tracker (M15/H1) determines the prevailing directional trend. Entries *against* the HTF bias are hard-blocked. |
| **Liquidity Sweep** | Price wicks through a swing high/low but closes back inside — a classic stop-hunt that precedes institutional reversals. |
| **Order Block (OB)** | The last opposing candle before a displacement move that broke structure. Marks institutional supply/demand zones. |
| **Fair Value Gap (FVG)** | A 3-candle imbalance where price left an unfilled gap. Acts as a magnet for price retracement. |
| **BOS / CHOCH** | Break of Structure (trend continuation) and Change of Character (trend reversal) confirmed only on candle *close*. |
| **Adaptive Swing Detection** | Swing points must exceed an ATR-scaled prominence threshold — suppresses noise in volatile conditions. |

### Dynamic Confluence Scoring (0–100)

Every potential entry is scored across 5 independent factors. **Only signals scoring ≥ 75 are accepted.**

```
┌───────────────────────────────────────┬────────┐
│ Factor                                │ Points │
├───────────────────────────────────────┼────────┤
│ 1. Liquidity Sweep (stop-hunt)        │   40   │
│ 2. Strong Rejection Candle            │   25   │
│ 3. BOS/CHOCH aligned with HTF bias    │   20   │
│ 4. Order Block proximity (at entry)   │   10   │
│ 5. Session active + Spread OK         │    5   │
├───────────────────────────────────────┼────────┤
│ TOTAL POSSIBLE                        │  100   │
│ MINIMUM TO FIRE                       │   75   │
└───────────────────────────────────────┴────────┘
```

The confluence score is:
- Logged in structured JSON for every accepted AND rejected signal
- Displayed in Telegram notifications: `Confluence: 82/100 • 4/5 factors`
- Stored in the trade journal via `SignalConfluence` dataclass
- Normalized to `Signal.confidence` (0.0–1.0) for downstream layers

### Entry Rules: LONG

```
Prerequisites (hard-blocks — instant reject):
  ✗ HTF bias is BEARISH → blocked
  ✗ Outside London/NY session → blocked
  ✗ No SWEEP_LOW event on this bar → skip

Scoring:
  ✓ Liquidity sweep below swing low                     → +40 pts
  ✓ Bullish rejection candle (long lower wick, small body) → +25 pts
  ✓ BOS_BULL or CHOCH_BULL + HTF BULLISH               → +20 pts
    (BOS + HTF NEUTRAL → +10 pts partial credit)
  ✓ Entry price inside an active Bullish Order Block    → +10 pts
  ✓ Session active                                      → +5 pts
  ◎ Entry inside Bullish FVG                            → tag only (journal)

Gate: score ≥ 75 → BUILD SIGNAL
  • Entry: candle close
  • SL: min(swept_low, candle.low) − 1 pip
  • TP: max(target_profit_pct × entry, min_rr × risk)
  • Final check: RR ≥ 1.2 or reject
```

### Entry Rules: SHORT

```
Prerequisites (hard-blocks — instant reject):
  ✗ HTF bias is BULLISH → blocked
  ✗ Outside London/NY session → blocked
  ✗ No SWEEP_HIGH event on this bar → skip

Scoring:
  ✓ Liquidity sweep above swing high                    → +40 pts
  ✓ Bearish rejection candle (long upper wick, small body) → +25 pts
  ✓ BOS_BEAR or CHOCH_BEAR + HTF BEARISH               → +20 pts
    (BOS + HTF NEUTRAL → +10 pts partial credit)
  ✓ Entry price inside an active Bearish Order Block    → +10 pts
  ✓ Session active                                      → +5 pts
  ◎ Entry inside Bearish FVG                            → tag only (journal)

Gate: score ≥ 75 → BUILD SIGNAL
  • Entry: candle close
  • SL: max(swept_high, candle.high) + 1 pip
  • TP: max(target_profit_pct × entry, min_rr × risk)
  • Final check: RR ≥ 1.2 or reject
```

### HTF Bias Integration

```python
# Engine wires it like this:
htf_tracker = HTFStructure(swing_lookback=3)  # one per symbol, on M15

# Whenever a new M15 bar closes:
htf_tracker.update(m15_candle)
strategy.set_htf_bias(htf_tracker.bias)  # inject into execution-TF strategy
```

The HTF bias is derived from the most recent BOS/CHOCH on the higher timeframe:
- Last HTF event was `BOS_BULL` or `CHOCH_BULL` → **BULLISH**
- Last HTF event was `BOS_BEAR` or `CHOCH_BEAR` → **BEARISH**
- No conclusive event yet → **NEUTRAL** (both directions allowed)

### Rejection Logging (for Strategy Improvement)

Every rejected signal evaluation is logged in structured JSON:

```json
{
  "message": "signal_rejected",
  "symbol": "EURUSD",
  "direction": "LONG",
  "rejection_reason": "CONFLUENCE_LOW (45<75)",
  "score": 45,
  "factors_hit": 2,
  "factors_total": 5,
  "sweep_pts": 40,
  "rejection_pts": 0,
  "bos_htf_pts": 0,
  "ob_pts": 0,
  "session_pts": 5,
  "tags": ["SWEEP_LOW", "SESSION_OK"]
}
```

This enables post-session analysis: which factors are consistently missing?
Should `min_confluence` be lowered? Is the HTF bias too strict?

---

## Risk Management

The risk manager is the **only** module authorized to size positions and approve trades:

- **Position sizing**: Dollar risk / (pip risk * pip value) = lots
- **Daily trade cap**: Hard stop at 5 trades/day
- **Daily loss cap**: If realized P&L hits -1% of starting balance, halt
- **Consecutive losses**: 3 losses in a row = stop for the day
- **Trailing DD**: If total drawdown breaches 5%, emergency halt
- **Spread filter**: Rejects signals when spread exceeds thresholds
- **Kill switch**: `/api/v1/risk/disable` endpoint for manual halt

---

## Dashboard API

The FastAPI dashboard runs on port 8080 (configurable):

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Liveness probe (always 200) |
| `/ready` | GET | Readiness (WS connected + risk enabled) |
| `/api/v1/risk` | GET | Risk manager snapshot |
| `/api/v1/risk/disable` | POST | Emergency kill switch |
| `/api/v1/trades` | GET | Recent journal entries |
| `/api/v1/trades/open` | GET | Currently open positions |
| `/api/v1/stats` | GET | Session/symbol stats + winrate |
| `/api/v1/stats/equity` | GET | Equity curve data |

---

## Backtesting

```bash
# Generate sample data
python -m scripts.generate_sample_data --symbol EURUSD --bars 20000

# Run backtest with custom spread
python -m scripts.run_backtest \
  --csv data/sample_EURUSD_M1.csv \
  --symbol EURUSD \
  --spread 1.2 \
  --slippage 0.5 \
  --output results/backtest_eurusd.json
```

The backtest engine:
- Replays candles through the full strategy + risk + execution stack
- Simulates SL/TP fills within each bar's high/low range
- Applies configurable spread and slippage
- Outputs winrate, max drawdown, equity curve, and per-session breakdown
- No look-ahead bias

---

## Testing

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run all tests
pytest

# With coverage
pytest --cov=app --cov-report=term-missing

# Linting
ruff check app/ tests/

# Type checking
mypy app/
```

---

## Design Principles

1. **Capital preservation first** - Every design decision favors safety over profit
2. **Async-native** - Built on asyncio from the ground up; no blocking I/O
3. **Modular & testable** - Each layer depends on protocols, not implementations
4. **Prop-firm safe** - Hard-coded guardrails prevent rule violations
5. **Observable** - Structured JSON logs, healthchecks, Telegram alerts
6. **Crash-resilient** - Persistent journal, auto-reconnect WS, graceful shutdown

---

## Anti-Patterns (Explicitly Avoided)

- No martingale or grid strategies
- No averaging down on losing positions
- No overtrading (hard 5/day cap)
- No high-frequency tick scalping
- No unsafe leverage multiplication
- No trading outside defined sessions

---

## License

See [LICENSE](LICENSE) for details.
