"""Broker abstraction.

The execution layer depends only on this Protocol, so swapping live
TradeLocker for the paper broker (or any future integration) is a
single-line change.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.models import Order


@runtime_checkable
class BrokerClient(Protocol):
    """Minimum async broker surface."""

    async def connect(self) -> None: ...
    async def close(self) -> None: ...

    async def get_account_balance(self) -> float: ...
    async def get_quote(self, symbol: str) -> tuple[float, float]:
        """Return ``(bid, ask)`` for ``symbol``."""
        ...

    async def place_order(self, order: Order) -> Order: ...
    async def cancel_order(self, broker_order_id: str) -> bool: ...
    async def close_position(self, symbol: str) -> bool: ...
