"""Broker integrations.

The :class:`BrokerClient` protocol describes the minimum surface needed by
the engine. ``TradeLockerClient`` is the production implementation;
``PaperBroker`` is a simulator used in paper-trading and backtests.
"""

from app.broker.base import BrokerClient
from app.broker.paper import PaperBroker
from app.broker.tradelocker import TradeLockerClient

__all__ = ["BrokerClient", "PaperBroker", "TradeLockerClient"]
