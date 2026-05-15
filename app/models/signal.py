"""Strategy output: a Signal describes a candidate trade idea."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.utils.time import to_utc


class SignalDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class Signal(BaseModel):
    """A candidate trade emitted by the strategy.

    The signal carries everything the risk + execution layers need:
    direction, intended entry, stop-loss, take-profit, and a free-form
    ``reason`` string capturing the structural context (BOS/CHOCH/sweep).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    timeframe: str
    direction: SignalDirection
    entry_price: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    reason: str
    session: str
    structure_tags: list[str] = Field(default_factory=list)
    generated_at: datetime
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @property
    def risk_distance(self) -> float:
        """Absolute price distance between entry and stop."""
        return abs(self.entry_price - self.stop_loss)

    @property
    def reward_distance(self) -> float:
        return abs(self.take_profit - self.entry_price)

    @property
    def rr_ratio(self) -> float:
        if self.risk_distance == 0:
            return 0.0
        return self.reward_distance / self.risk_distance

    @classmethod
    def long(
        cls,
        *,
        symbol: str,
        timeframe: str,
        entry: float,
        sl: float,
        tp: float,
        reason: str,
        session: str,
        tags: list[str] | None = None,
        generated_at: datetime,
        confidence: float = 0.0,
    ) -> Signal:
        if not (sl < entry < tp):
            raise ValueError(f"LONG requires sl < entry < tp; got sl={sl} entry={entry} tp={tp}")
        return cls(
            symbol=symbol,
            timeframe=timeframe,
            direction=SignalDirection.LONG,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            reason=reason,
            session=session,
            structure_tags=tags or [],
            generated_at=to_utc(generated_at),
            confidence=confidence,
        )

    @classmethod
    def short(
        cls,
        *,
        symbol: str,
        timeframe: str,
        entry: float,
        sl: float,
        tp: float,
        reason: str,
        session: str,
        tags: list[str] | None = None,
        generated_at: datetime,
        confidence: float = 0.0,
    ) -> Signal:
        if not (tp < entry < sl):
            raise ValueError(f"SHORT requires tp < entry < sl; got sl={sl} entry={entry} tp={tp}")
        return cls(
            symbol=symbol,
            timeframe=timeframe,
            direction=SignalDirection.SHORT,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            reason=reason,
            session=session,
            structure_tags=tags or [],
            generated_at=to_utc(generated_at),
            confidence=confidence,
        )
