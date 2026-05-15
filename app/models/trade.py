"""Trade lifecycle record - the canonical row written to the journal."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.models.signal import SignalDirection


class TradeStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED_WIN = "CLOSED_WIN"
    CLOSED_LOSS = "CLOSED_LOSS"
    CLOSED_BE = "CLOSED_BE"     # break-even
    CANCELLED = "CANCELLED"


class Trade(BaseModel):
    """A complete trade record persisted to the journal."""

    model_config = ConfigDict(extra="forbid")

    trade_id: str
    symbol: str
    direction: SignalDirection
    entry_time: datetime
    entry_price: float
    stop_loss: float
    take_profit: float
    quantity: float
    exit_time: datetime | None = None
    exit_price: float | None = None
    pnl: float | None = None
    pnl_pct: float | None = None
    status: TradeStatus = TradeStatus.OPEN

    # journal-only fields
    session: str
    entry_reason: str
    exit_reason: str | None = None
    structure_state: dict[str, object] = Field(default_factory=dict)
    spread_pips: float | None = None
    slippage_pips: float | None = None
    latency_ms: float | None = None
    rr_ratio: float | None = None
    screenshot_path: str | None = None  # placeholder hook
