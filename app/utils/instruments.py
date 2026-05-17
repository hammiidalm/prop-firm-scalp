"""Instrument metadata: pip size, contract size, spread limits.

Centralizes the per-symbol arithmetic so risk/sizing/spread checks aren't
duplicated across the codebase.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    pip_size: float           # price delta of one pip
    contract_size: float      # units per 1.0 lot
    quote_per_pip_per_lot: float  # account-currency P&L for 1 pip * 1.0 lot
    is_metal: bool = False

    def pips(self, price_delta: float) -> float:
        """Convert an absolute price delta to pips."""
        return price_delta / self.pip_size

    def price_delta(self, pips: float) -> float:
        """Convert pips to an absolute price delta."""
        return pips * self.pip_size


# Quote-per-pip values assume USD-denominated account.
# EURUSD: 1 pip on 1.0 standard lot = $10
# XAUUSD: 1 pip (= $0.01) on 1.0 lot (100 oz) = $1; we redefine "pip" as $0.10
# move (10 cents) which gives $10/pip/lot, matching the FX convention.
_REGISTRY: dict[str, Instrument] = {
    "EURUSD": Instrument(
        symbol="EURUSD",
        pip_size=0.0001,
        contract_size=100_000,
        quote_per_pip_per_lot=10.0,
        is_metal=False,
    ),
    "GBPUSD": Instrument(
        symbol="GBPUSD",
        pip_size=0.0001,
        contract_size=100_000,
        quote_per_pip_per_lot=10.0,
    ),
    "USDJPY": Instrument(
        symbol="USDJPY",
        pip_size=0.01,
        contract_size=100_000,
        quote_per_pip_per_lot=9.0,  # approximate, USD/JPY varies with quote
    ),
    "XAUUSD": Instrument(
        symbol="XAUUSD",
        pip_size=0.10,
        contract_size=100,
        quote_per_pip_per_lot=10.0,
        is_metal=True,
    ),
    # Crypto — 24/7 markets, different pip conventions
    "BTCUSD": Instrument(
        symbol="BTCUSD",
        pip_size=1.0,
        contract_size=1,
        quote_per_pip_per_lot=1.0,
        is_metal=False,
    ),
    "ETHUSD": Instrument(
        symbol="ETHUSD",
        pip_size=0.10,
        contract_size=1,
        quote_per_pip_per_lot=1.0,
        is_metal=False,
    ),
}


def get_instrument(symbol: str) -> Instrument:
    sym = symbol.upper()
    if sym not in _REGISTRY:
        raise KeyError(f"Unknown instrument: {symbol}. Register it in app.utils.instruments.")
    return _REGISTRY[sym]


def register_instrument(inst: Instrument) -> None:
    """Add/override an instrument at runtime (useful for tests or new symbols)."""
    _REGISTRY[inst.symbol.upper()] = inst
