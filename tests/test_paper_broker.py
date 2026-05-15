"""Tests for the PaperBroker."""

import pytest

from app.broker.paper import PaperBroker
from app.models import Order, OrderSide, OrderStatus, OrderType


@pytest.fixture
def broker():
    b = PaperBroker(starting_balance=100_000.0)
    b.set_quote("EURUSD", 1.0849, 1.0851)
    return b


class TestPaperBroker:
    @pytest.mark.asyncio
    async def test_connect(self, broker):
        await broker.connect()
        assert broker.balance == 100_000.0

    @pytest.mark.asyncio
    async def test_get_balance(self, broker):
        balance = await broker.get_account_balance()
        assert balance == 100_000.0

    @pytest.mark.asyncio
    async def test_get_quote(self, broker):
        bid, ask = await broker.get_quote("EURUSD")
        assert bid == pytest.approx(1.0849)
        assert ask == pytest.approx(1.0851)

    @pytest.mark.asyncio
    async def test_get_quote_missing_symbol_raises(self, broker):
        with pytest.raises(KeyError):
            await broker.get_quote("UNKNOWN")

    @pytest.mark.asyncio
    async def test_place_buy_order(self, broker):
        await broker.connect()
        order = Order(
            client_order_id="test-001",
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=0.10,
        )
        filled = await broker.place_order(order)
        assert filled.status is OrderStatus.FILLED
        assert filled.filled_price == pytest.approx(1.0851)  # filled at ask
        assert filled.broker_order_id is not None

    @pytest.mark.asyncio
    async def test_place_sell_order(self, broker):
        await broker.connect()
        order = Order(
            client_order_id="test-002",
            symbol="EURUSD",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=0.10,
        )
        filled = await broker.place_order(order)
        assert filled.filled_price == pytest.approx(1.0849)  # filled at bid

    @pytest.mark.asyncio
    async def test_close_position(self, broker):
        await broker.connect()
        order = Order(
            client_order_id="test-003",
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=1.0,
        )
        await broker.place_order(order)
        # Move price up
        broker.set_quote("EURUSD", 1.0859, 1.0861)
        closed = await broker.close_position("EURUSD")
        assert closed is True
        # Balance should have increased (bought at 1.0851, sold at 1.0859 bid)
        assert broker.balance > 100_000.0
