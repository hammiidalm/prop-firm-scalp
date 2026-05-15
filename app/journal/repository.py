"""Async repository wrapping SQLAlchemy 2.x AsyncSession."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.journal.models import Base, TradeRow
from app.models import Trade
from app.utils.logging import get_logger

log = get_logger(__name__)


class TradeJournal:
    """Persistence layer for ``Trade`` records.

    Supports both SQLite (default) and Postgres via the ``DATABASE_URL``
    setting. ``upsert`` semantics are used so re-saving an open trade after
    it closes simply updates the existing row.
    """

    def __init__(self, database_url: str) -> None:
        self._url = database_url
        self._engine = create_async_engine(database_url, future=True, pool_pre_ping=True)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._is_sqlite = database_url.startswith("sqlite")

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        log.info("journal initialized url=%s", self._url.split("://")[0])

    async def close(self) -> None:
        await self._engine.dispose()

    async def save(self, trade: Trade) -> None:
        data = self._to_row_dict(trade)
        async with self._session_factory() as session:
            if self._is_sqlite:
                stmt = sqlite_insert(TradeRow).values(**data)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[TradeRow.trade_id],
                    set_={k: v for k, v in data.items() if k != "trade_id"},
                )
                await session.execute(stmt)
            else:
                # Postgres path: try update first, fall back to insert.
                existing = await session.scalar(
                    select(TradeRow).where(TradeRow.trade_id == trade.trade_id)
                )
                if existing:
                    for k, v in data.items():
                        setattr(existing, k, v)
                else:
                    session.add(TradeRow(**data))
            await session.commit()

    async def list_recent(self, limit: int = 100) -> Sequence[TradeRow]:
        async with self._session_factory() as session:
            res = await session.execute(
                select(TradeRow).order_by(TradeRow.entry_time.desc()).limit(limit)
            )
            return res.scalars().all()

    async def session(self) -> AsyncSession:
        return self._session_factory()

    @staticmethod
    def _to_row_dict(trade: Trade) -> dict:
        return {
            "trade_id": trade.trade_id,
            "symbol": trade.symbol,
            "direction": trade.direction.value,
            "entry_time": trade.entry_time,
            "entry_price": trade.entry_price,
            "stop_loss": trade.stop_loss,
            "take_profit": trade.take_profit,
            "quantity": trade.quantity,
            "exit_time": trade.exit_time,
            "exit_price": trade.exit_price,
            "pnl": trade.pnl,
            "pnl_pct": trade.pnl_pct,
            "status": trade.status.value,
            "session": trade.session,
            "entry_reason": trade.entry_reason,
            "exit_reason": trade.exit_reason,
            "structure_state": trade.structure_state,
            "spread_pips": trade.spread_pips,
            "slippage_pips": trade.slippage_pips,
            "latency_ms": trade.latency_ms,
            "rr_ratio": trade.rr_ratio,
            "screenshot_path": trade.screenshot_path,
        }
