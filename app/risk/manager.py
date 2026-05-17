"""Prop-firm risk manager.

This module is the *only* place authorized to size positions and to allow
or block new trades. The execution layer must call ``evaluate`` for every
signal and respect the result.

Guards enforced
---------------
1. **Daily trade cap** - ``max_trades_per_day`` (default 5).
2. **Daily loss cap** - if realized loss for the UTC day exceeds
   ``max_daily_loss_pct`` of starting balance, trading is suspended until
   the next UTC midnight.
3. **Consecutive losses circuit-breaker** - after N losses in a row, the
   manager halts new trades for the rest of the day.
4. **Total drawdown** - protects the prop-firm trailing-DD threshold.
5. **Spread filter** - delegated to execution but exposed via the
   ``RiskManager.is_spread_acceptable`` helper.
6. **Position sizing** - dollar risk = ``risk_per_trade_pct * balance``,
   converted to lots given the per-instrument pip value.

The manager is fully synchronous and dependency-free so it can be unit
tested without the broker.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

from app.config import Settings
from app.models import Signal, SignalDirection, Trade, TradeStatus
from app.utils.instruments import get_instrument
from app.utils.logging import get_logger
from app.utils.time import utcnow

log = get_logger(__name__)


class RiskRejectReason(str, Enum):
    OK = "OK"
    DAILY_TRADE_LIMIT = "DAILY_TRADE_LIMIT"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    TOTAL_DRAWDOWN = "TOTAL_DRAWDOWN"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    INVALID_SIGNAL = "INVALID_SIGNAL"
    SIZE_TOO_SMALL = "SIZE_TOO_SMALL"
    DISABLED = "DISABLED"
    CONSISTENCY_RULE = "CONSISTENCY_RULE"


@dataclass(frozen=True, slots=True)
class RiskDecision:
    accepted: bool
    quantity_lots: float
    risk_amount: float
    reason: RiskRejectReason
    detail: str = ""


@dataclass(slots=True)
class RiskManager:
    settings: Settings
    starting_balance: float
    current_balance: float = 0.0
    high_water_mark: float = 0.0

    _today: date = field(default_factory=lambda: utcnow().date(), init=False)
    _trades_today: int = field(default=0, init=False)
    _realized_today: float = field(default=0.0, init=False)
    _consecutive_losses: int = field(default=0, init=False)
    _disabled: bool = field(default=False, init=False)
    _trading_start_date: date | None = field(default=None, init=False)
    _trading_days_counted: int = field(default=0, init=False)
    _highest_day_pnl: float = field(default=0.0, init=False)
    _simulated_now: date | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.current_balance <= 0:
            self.current_balance = self.starting_balance
        if self.high_water_mark <= 0:
            self.high_water_mark = self.starting_balance
        if self._simulated_now is None:
            self._today = utcnow().date()

    def set_simulated_time(self, dt: datetime) -> None:
        """Override internal clock for backtesting. Pass candle timestamps."""
        self._simulated_now = dt.date()

    def _now_date(self) -> date:
        return self._simulated_now if self._simulated_now is not None else utcnow().date()

    # ---- day rollover ---------------------------------------------------
    def _maybe_rollover(self) -> None:
        today = self._now_date()
        if today != self._today:
            log.info(
                "risk: day rollover",
                extra={"prev_day": str(self._today), "trades": self._trades_today, "pnl": self._realized_today},
            )
            # Count trading days (days that had at least 1 trade)
            if self._trades_today > 0:
                self._trading_days_counted += 1
            # Track highest profitable day (for consistency rule)
            if self._realized_today > self._highest_day_pnl:
                self._highest_day_pnl = self._realized_today
            self._today = today
            self._trades_today = 0
            self._realized_today = 0.0
            # Reset the consecutive-loss circuit breaker each new day.
            self._consecutive_losses = 0
            self._disabled = False

    # ---- public surface -------------------------------------------------
    def evaluate(self, signal: Signal, *, spread_pips: float | None = None) -> RiskDecision:
        """Decide whether to accept ``signal`` and at what size."""
        self._maybe_rollover()

        if self._disabled:
            return RiskDecision(False, 0.0, 0.0, RiskRejectReason.DISABLED,
                                "risk manager halted for the day")

        if self._trades_today >= self.settings.max_trades_per_day:
            return RiskDecision(False, 0.0, 0.0, RiskRejectReason.DAILY_TRADE_LIMIT,
                                f"already {self._trades_today} trades today")

        if self._consecutive_losses >= self.settings.max_consecutive_losses:
            self._disabled = True
            return RiskDecision(False, 0.0, 0.0, RiskRejectReason.CONSECUTIVE_LOSSES,
                                f"{self._consecutive_losses} losses in a row")

        if self._realized_today <= -self.settings.daily_loss_limit_amount():
            self._disabled = True
            return RiskDecision(False, 0.0, 0.0, RiskRejectReason.DAILY_LOSS_LIMIT,
                                f"daily DD {self._realized_today:.2f} hit cap")

        dd_pct = (self.high_water_mark - self.current_balance) / self.high_water_mark
        if dd_pct >= self.settings.max_total_dd_pct:
            self._disabled = True
            return RiskDecision(False, 0.0, 0.0, RiskRejectReason.TOTAL_DRAWDOWN,
                                f"trailing DD {dd_pct:.2%} >= cap")

        # ---- Consistency rule: best day ≤ consistency% of total profit -----
        total_profit = self.current_balance - self.starting_balance
        if total_profit > 0 and self._realized_today > self.settings.consistency_pct * total_profit:
            self._disabled = True
            return RiskDecision(False, 0.0, 0.0, RiskRejectReason.CONSISTENCY_RULE,
                                f"today's PnL ${self._realized_today:.2f} exceeds "
                                f"{self.settings.consistency_pct:.0%} of total profit ${total_profit:.2f}")

        if spread_pips is not None and not self.is_spread_acceptable(signal.symbol, spread_pips):
            return RiskDecision(False, 0.0, 0.0, RiskRejectReason.SPREAD_TOO_WIDE,
                                f"spread {spread_pips:.2f} pips too wide")

        if signal.risk_distance <= 0:
            return RiskDecision(False, 0.0, 0.0, RiskRejectReason.INVALID_SIGNAL,
                                "risk distance is zero")

        # ---- position sizing ---------------------------------------------
        lots = self._size_position(signal)
        if lots <= 0:
            return RiskDecision(False, 0.0, 0.0, RiskRejectReason.SIZE_TOO_SMALL,
                                "computed size <= 0; check min lot / pip value")

        risk_amount = self.current_balance * self.settings.risk_per_trade_pct
        return RiskDecision(True, lots, risk_amount, RiskRejectReason.OK,
                            f"sized {lots} lots, risking ${risk_amount:.2f}")

    def is_min_trading_days_met(self) -> bool:
        """Check if minimum trading days requirement is satisfied."""
        return self._trading_days_counted >= self.settings.min_trading_days

    def is_spread_acceptable(self, symbol: str, spread_pips: float) -> bool:
        inst = get_instrument(symbol)
        cap = (
            self.settings.max_spread_pips_metals
            if inst.is_metal
            else self.settings.max_spread_pips_fx
        )
        return spread_pips <= cap

    # ---- account updates from execution layer ---------------------------
    def register_trade_open(self) -> None:
        self._maybe_rollover()
        if self._trading_start_date is None:
            self._trading_start_date = self._now_date()
        self._trades_today += 1

    def register_trade_close(self, trade: Trade) -> None:
        self._maybe_rollover()
        pnl = trade.pnl or 0.0
        self.current_balance += pnl
        self._realized_today += pnl
        if self._realized_today > self._highest_day_pnl:
            self._highest_day_pnl = self._realized_today
        self.high_water_mark = max(self.high_water_mark, self.current_balance)
        if trade.status is TradeStatus.CLOSED_LOSS or pnl < 0:
            self._consecutive_losses += 1
        elif trade.status is TradeStatus.CLOSED_WIN or pnl > 0:
            self._consecutive_losses = 0
        log.info(
            "risk: trade closed",
            extra={
                "pnl": pnl,
                "balance": self.current_balance,
                "trades_today": self._trades_today,
                "realized_today": self._realized_today,
                "consec_losses": self._consecutive_losses,
            },
        )

    def force_disable(self, reason: str = "manual") -> None:
        self._disabled = True
        log.warning("risk manager force-disabled: %s", reason)

    # ---- introspection (used by dashboard) ------------------------------
    def snapshot(self) -> dict[str, object]:
        self._maybe_rollover()
        return {
            "date": str(self._today),
            "balance": self.current_balance,
            "high_water_mark": self.high_water_mark,
            "trades_today": self._trades_today,
            "realized_today": self._realized_today,
            "consecutive_losses": self._consecutive_losses,
            "disabled": self._disabled,
            "drawdown_pct": (self.high_water_mark - self.current_balance) / self.high_water_mark
            if self.high_water_mark
            else 0.0,
            "trading_days_counted": self._trading_days_counted,
            "min_trading_days": self.settings.min_trading_days,
            "trading_days_met": self._trading_days_counted >= self.settings.min_trading_days,
            "trading_start_date": str(self._trading_start_date) if self._trading_start_date else None,
            "consistency_pct": self.settings.consistency_pct,
            "consistency_check": f"best day ≤ {self.settings.consistency_pct:.0%} of total profit",
            "highest_day_pnl": round(self._highest_day_pnl, 2),
        }

    # ---- internal -------------------------------------------------------
    def _size_position(self, signal: Signal) -> float:
        inst = get_instrument(signal.symbol)
        risk_amount = self.current_balance * self.settings.risk_per_trade_pct
        risk_pips = inst.pips(signal.risk_distance)
        if risk_pips <= 0:
            return 0.0
        # account-currency loss per 1 lot if SL is hit
        loss_per_lot = risk_pips * inst.quote_per_pip_per_lot
        if loss_per_lot <= 0:
            return 0.0
        raw_lots = risk_amount / loss_per_lot
        if signal.direction is SignalDirection.LONG and raw_lots <= 0:
            return 0.0
        # Round down to 2 decimal places (mini-lots). Adjust if your broker
        # supports 0.001 (micro) lots.
        return math.floor(raw_lots * 100) / 100
