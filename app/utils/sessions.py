"""Trading-session filters.

Prop-firm scalping is highly session-dependent: liquidity is concentrated
during London (07:00-11:00 UTC) and New York (12:00-16:00 UTC). Outside
these windows, spreads widen and structure becomes unreliable, so the
strategy must refuse new entries.

This module exposes a small ``SessionFilter`` that the strategy and
execution layers consult before generating or sending orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from app.utils.time import to_utc


class Session(str, Enum):
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"
    OVERLAP = "OVERLAP"  # London + NY overlap (highest liquidity)
    OFF = "OFF"


@dataclass(frozen=True, slots=True)
class SessionFilter:
    london_open_utc: int
    london_close_utc: int
    ny_open_utc: int
    ny_close_utc: int
    _24h_symbols: tuple[str, ...] = ("BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD")  # 24/7 markets

    def classify(self, ts: datetime) -> Session:
        """Return the session that ``ts`` (UTC) falls into."""
        hour = to_utc(ts).hour
        in_london = self.london_open_utc <= hour < self.london_close_utc
        in_ny = self.ny_open_utc <= hour < self.ny_close_utc
        if in_london and in_ny:
            return Session.OVERLAP
        if in_london:
            return Session.LONDON
        if in_ny:
            return Session.NEW_YORK
        return Session.OFF

    def is_active(self, ts: datetime, symbol: str = "") -> bool:
        """Return True if session is active, or symbol is a 24/7 market."""
        if symbol.upper() in self._24h_symbols:
            return True
        return self.classify(ts) is not Session.OFF
