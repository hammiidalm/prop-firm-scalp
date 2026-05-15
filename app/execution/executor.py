"""Signal -> Order translation, with paper / semi-auto / full-auto modes.

Modes
-----
* **paper**     - everything goes through the ``PaperBroker``; no real money.
* **semi_auto** - the bot detects + sends a Telegram signal; the user
  manually confirms (the executor exposes ``confirm_pending`` that the
  notifier or dashboard calls when the trader replies).
* **full_auto** - signals approved by the risk manager are sent straight
  to the broker.

The executor never bypasses the risk manager.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.broker.base import BrokerClient
from app.config.settings import TradingMode
from app.models import Order, OrderSide, OrderStatus, OrderType, Signal, Trade, TradeStatus
from app.models.signal import SignalDirection
from app.risk import RiskDecision, RiskManager, RiskRejectReason
from app.utils.instruments import get_instrument
from app.utils.logging import get_logger
from app.utils.time import utcnow

log = get_logger(__name__)

NotifyFn = Callable[[str, dict[str, object]], Awaitable[None]]
PersistTradeFn = Callable[[Trade], Awaitable[None]]


@dataclass(slots=True)
class _Pending:
    signal: Signal
    decision: RiskDecision
    created_at: datetime
    expires_at: datetime


class Executor:
    def __init__(
        self,
        *,
        broker: BrokerClient,
        risk: RiskManager,
        mode: TradingMode,
        notify: NotifyFn | None = None,
        persist_trade: PersistTradeFn | None = None,
        semi_auto_ttl_sec: float = 90.0,
    ) -> None:
        self._broker = broker
        self._risk = risk
        self._mode = mode
        self._notify = notify or self._noop_notify
        self._persist = persist_trade or self._noop_persist
        self._semi_auto_ttl_sec = semi_auto_ttl_sec
        self._pending: dict[str, _Pending] = {}
        self._open_trades: dict[str, Trade] = {}
        self._lock = asyncio.Lock()

    # ---- public ---------------------------------------------------------
    async def handle_signal(self, signal: Signal, *, spread_pips: float | None = None) -> Trade | None:
        """Process a strategy signal end-to-end."""
        decision = self._risk.evaluate(signal, spread_pips=spread_pips)
        if not decision.accepted:
            log.info(
                "signal rejected by risk",
                extra={"reason": decision.reason.value, "detail": decision.detail},
            )
            await self._notify("risk_reject", {
                "symbol": signal.symbol,
                "reason": decision.reason.value,
                "detail": decision.detail,
            })
            return None

        if self._mode is TradingMode.semi_auto:
            return await self._stage_semi_auto(signal, decision)
        return await self._fire(signal, decision)

    async def confirm_pending(self, pending_id: str) -> Trade | None:
        """Used by Telegram/dashboard to confirm a semi-auto signal."""
        async with self._lock:
            pending = self._pending.pop(pending_id, None)
        if pending is None:
            log.warning("semi-auto confirm: id %s not found / expired", pending_id)
            return None
        if utcnow() > pending.expires_at:
            log.warning("semi-auto confirm: expired")
            return None
        return await self._fire(pending.signal, pending.decision)

    async def cancel_pending(self, pending_id: str) -> bool:
        async with self._lock:
            return self._pending.pop(pending_id, None) is not None

    def open_trades(self) -> dict[str, Trade]:
        return dict(self._open_trades)

    # ---- internals ------------------------------------------------------
    async def _stage_semi_auto(self, signal: Signal, decision: RiskDecision) -> None:
        pending_id = uuid4().hex[:10]
        async with self._lock:
            self._pending[pending_id] = _Pending(
                signal=signal,
                decision=decision,
                created_at=utcnow(),
                expires_at=utcnow().fromtimestamp(utcnow().timestamp() + self._semi_auto_ttl_sec),
            )
        await self._notify("semi_auto_pending", {
            "pending_id": pending_id,
            "symbol": signal.symbol,
            "direction": signal.direction.value,
            "entry": signal.entry_price,
            "sl": signal.stop_loss,
            "tp": signal.take_profit,
            "rr": signal.rr_ratio,
            "lots": decision.quantity_lots,
            "ttl_sec": self._semi_auto_ttl_sec,
        })
        log.info("semi-auto pending %s", pending_id)
        return None

    async def _fire(self, signal: Signal, decision: RiskDecision) -> Trade | None:
        side = OrderSide.BUY if signal.direction is SignalDirection.LONG else OrderSide.SELL
        order = Order(
            client_order_id=uuid4().hex,
            symbol=signal.symbol,
            side=side,
            order_type=OrderType.MARKET,
            quantity=decision.quantity_lots,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
        )

        t0 = utcnow()
        try:
            placed = await self._with_retry(order)
        except Exception as exc:  # noqa: BLE001
            log.exception("order placement permanently failed: %s", exc)
            await self._notify("order_failed", {"symbol": signal.symbol, "error": str(exc)})
            return None

        if placed.status not in (OrderStatus.FILLED, OrderStatus.SUBMITTED):
            log.warning("order not accepted status=%s", placed.status)
            return None

        latency_ms = (utcnow() - t0).total_seconds() * 1000.0
        inst = get_instrument(signal.symbol)
        # spread approximation: difference between intended entry and fill
        slippage_pips = (
            inst.pips(abs((placed.filled_price or signal.entry_price) - signal.entry_price))
        )

        trade = Trade(
            trade_id=order.client_order_id,
            symbol=signal.symbol,
            direction=signal.direction,
            entry_time=placed.filled_at or t0,
            entry_price=placed.filled_price or signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            quantity=decision.quantity_lots,
            status=TradeStatus.OPEN,
            session=signal.session,
            entry_reason=signal.reason,
            structure_state={"tags": signal.structure_tags},
            slippage_pips=slippage_pips,
            latency_ms=latency_ms,
            rr_ratio=signal.rr_ratio,
        )

        self._risk.register_trade_open()
        self._open_trades[trade.trade_id] = trade
        await self._persist(trade)
        await self._notify("trade_open", trade.model_dump(mode="json"))
        log.info(
            "trade opened",
            extra={
                "trade_id": trade.trade_id,
                "symbol": trade.symbol,
                "side": side.value,
                "lots": trade.quantity,
                "latency_ms": latency_ms,
            },
        )
        return trade

    async def close_trade(self, trade_id: str, *, reason: str, exit_price: float) -> Trade | None:
        trade = self._open_trades.pop(trade_id, None)
        if trade is None:
            return None
        inst = get_instrument(trade.symbol)
        direction = 1 if trade.direction is SignalDirection.LONG else -1
        price_pnl = (exit_price - trade.entry_price) * direction
        pnl = inst.pips(price_pnl) * inst.quote_per_pip_per_lot * trade.quantity
        if pnl > 0:
            status = TradeStatus.CLOSED_WIN
        elif pnl < 0:
            status = TradeStatus.CLOSED_LOSS
        else:
            status = TradeStatus.CLOSED_BE
        closed = trade.model_copy(update={
            "exit_time": utcnow(),
            "exit_price": exit_price,
            "pnl": pnl,
            "pnl_pct": pnl / max(self._risk.current_balance, 1.0),
            "status": status,
            "exit_reason": reason,
        })
        self._risk.register_trade_close(closed)
        await self._persist(closed)
        await self._notify("trade_close", closed.model_dump(mode="json"))
        return closed

    async def _with_retry(self, order: Order) -> Order:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.3, max=2),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        ):
            with attempt:
                placed = await self._broker.place_order(order)
                if placed.status is OrderStatus.REJECTED:
                    raise RuntimeError(f"rejected: {placed.rejection_reason}")
                return placed
        raise RuntimeError("unreachable")

    @staticmethod
    async def _noop_notify(event: str, payload: dict[str, object]) -> None:
        return None

    @staticmethod
    async def _noop_persist(trade: Trade) -> None:
        return None
