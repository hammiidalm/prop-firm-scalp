"""Order primitives sent to the broker."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class Order(BaseModel):
    """Outbound order request / state."""

    model_config = ConfigDict(extra="forbid")

    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float = Field(gt=0)  # broker units (lots)
    price: float | None = None     # required for LIMIT/STOP
    stop_loss: float | None = None
    take_profit: float | None = None
    status: OrderStatus = OrderStatus.PENDING
    broker_order_id: str | None = None
    submitted_at: datetime | None = None
    filled_price: float | None = None
    filled_at: datetime | None = None
    rejection_reason: str | None = None
