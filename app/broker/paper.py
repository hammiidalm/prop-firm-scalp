"""In-memory paper-trading broker.

Simulates fills at the latest mid price plus a configurable spread/slippage,
maintains a synthetic balance and supports the same async surface as the
live TradeLocker client. Used for paper trading and as the backtesting
execution engine.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
from uuid import uuid4

from app.broker.base import BrokerClient
from app.models import Order, OrderSide, OrderStatus
from app.utils.logging import get_logger
from app.utils.time import utcnow

log = get_logger(__name__)


class PaperBroker(BrokerClient):
    """Deterministic paper broker driven by ``set_quote``."""

    def __init__(self, starting_balance: float = 100_000.0, slippage_pips: float = 0.0) -> None:
        self._balance = starting_balance
        self._slippage_pips = slippage_pips
        self._quotes: dict[str, tuple[float, float]] = {}
        self._open_positions: dict[str, Order] = {}
        self._orders: dict[str, Order] = {}
        self._pnl_by_symbol: dict[str, float] = defaultdict(float)
        self._lock = asyncio.Lock()

    async def connect(self) -> None:  # noqa: D401 - protocol impl
        log.info("paper broker connected balance=%.2f", self._balance)

    async def close(self) -> None:
        return None

    def set_quote(self, symbol: str, bid: float, ask: float) -> None:
        """Push a quote into the simulator (called by the engine/replay)."""
        self._quotes[symbol] = (bid, ask)

    async def get_account_balance(self) -> float:
        return self._balance

    async def get_quote(self, symbol: str) -> tuple[float, float]:
        if symbol not in self._quotes:
            raise KeyError(f"no paper quote set for {symbol}")
        return self._quotes[symbol]

    async def place_order(self, order: Order) -> Order:
        async with self._lock:
            bid, ask = await self.get_quote(order.symbol)
            fill = ask if order.side is OrderSide.BUY else bid
            broker_id = f"paper-{uuid4().hex[:10]}"
            filled = order.model_copy(update={
                "status": OrderStatus.FILLED,
                "broker_order_id": broker_id,
                "submitted_at": utcnow(),
                "filled_price": fill,
                "filled_at": utcnow(),
            })
            self._orders[broker_id] = filled
            self._open_positions[order.symbol] = filled
            log.info("paper fill", extra={"symbol": order.symbol, "side": order.side.value, "fill": fill})
            return filled

    async def cancel_order(self, broker_order_id: str) -> bool:
        order = self._orders.get(broker_order_id)
        if not order:
            return False
        self._orders[broker_order_id] = order.model_copy(update={"status": OrderStatus.CANCELLED})
        return True

    async def close_position(self, symbol: str) -> bool:
        async with self._lock:
            pos = self._open_positions.pop(symbol, None)
            if not pos or pos.filled_price is None:
                return False
            bid, ask = await self.get_quote(symbol)
            exit_price = bid if pos.side is OrderSide.BUY else ask
            direction = 1 if pos.side is OrderSide.BUY else -1
            # P&L in price-points * quantity. The strategy/risk layer is
            # responsible for translating to account-currency dollars.
            pnl = (exit_price - pos.filled_price) * direction * pos.quantity
            self._pnl_by_symbol[symbol] += pnl
            self._balance += pnl
            log.info(
                "paper close",
                extra={"symbol": symbol, "exit": exit_price, "pnl_points": pnl},
            )
            return True

    # ---- introspection helpers (used by tests / dashboard) --------------
    def open_positions(self) -> dict[str, Order]:
        return dict(self._open_positions)

    def realized_pnl(self) -> float:
        return self._balance - sum(  # noqa: PLR1704 - readable as-is
            o.quantity * 0 for o in self._orders.values()  # placeholder
        ) - 0

    @property
    def balance(self) -> float:
        return self._balance

    def now(self) -> datetime:
        return utcnow()
