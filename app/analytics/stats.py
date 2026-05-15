"""In-memory trade-stats aggregator (winrate, equity curve, session breakdown)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.models import Trade, TradeStatus


@dataclass(slots=True)
class SessionStats:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0

    @property
    def winrate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0


@dataclass(slots=True)
class TradeStatsAggregator:
    starting_balance: float
    equity_curve: list[float] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    by_session: dict[str, SessionStats] = field(default_factory=lambda: defaultdict(SessionStats))
    by_symbol: dict[str, SessionStats] = field(default_factory=lambda: defaultdict(SessionStats))

    def __post_init__(self) -> None:
        if not self.equity_curve:
            self.equity_curve.append(self.starting_balance)

    def record(self, trade: Trade) -> None:
        if trade.status is TradeStatus.OPEN:
            return
        self.trades.append(trade)
        pnl = trade.pnl or 0.0
        self.equity_curve.append(self.equity_curve[-1] + pnl)
        for bucket in (self.by_session[trade.session], self.by_symbol[trade.symbol]):
            bucket.trades += 1
            bucket.pnl += pnl
            if trade.status is TradeStatus.CLOSED_WIN or pnl > 0:
                bucket.wins += 1
            elif trade.status is TradeStatus.CLOSED_LOSS or pnl < 0:
                bucket.losses += 1

    # ---- aggregates -----------------------------------------------------
    @property
    def total_pnl(self) -> float:
        return self.equity_curve[-1] - self.starting_balance if self.equity_curve else 0.0

    @property
    def winrate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if (t.pnl or 0) > 0)
        return wins / len(self.trades)

    @property
    def max_drawdown(self) -> float:
        peak = self.starting_balance
        max_dd = 0.0
        for v in self.equity_curve:
            peak = max(peak, v)
            dd = (peak - v) / peak if peak else 0.0
            max_dd = max(max_dd, dd)
        return max_dd

    def summary(self) -> dict[str, object]:
        return {
            "trades": len(self.trades),
            "winrate": self.winrate,
            "total_pnl": self.total_pnl,
            "final_equity": self.equity_curve[-1] if self.equity_curve else self.starting_balance,
            "max_drawdown_pct": self.max_drawdown,
            "by_session": {k: v.__dict__ for k, v in self.by_session.items()},
            "by_symbol": {k: v.__dict__ for k, v in self.by_symbol.items()},
        }
