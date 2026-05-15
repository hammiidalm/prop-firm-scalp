"""Tests for instrument utilities."""

import pytest

from app.utils.instruments import Instrument, get_instrument, register_instrument


class TestInstruments:
    def test_get_eurusd(self):
        inst = get_instrument("EURUSD")
        assert inst.pip_size == pytest.approx(0.0001)
        assert inst.contract_size == 100_000
        assert not inst.is_metal

    def test_get_xauusd(self):
        inst = get_instrument("XAUUSD")
        assert inst.pip_size == pytest.approx(0.10)
        assert inst.is_metal

    def test_pips_conversion(self):
        inst = get_instrument("EURUSD")
        assert inst.pips(0.0010) == pytest.approx(10.0)
        assert inst.price_delta(10.0) == pytest.approx(0.0010)

    def test_unknown_instrument_raises(self):
        with pytest.raises(KeyError, match="Unknown instrument"):
            get_instrument("NOPE")

    def test_register_custom_instrument(self):
        custom = Instrument(
            symbol="AUDNZD",
            pip_size=0.0001,
            contract_size=100_000,
            quote_per_pip_per_lot=7.0,
        )
        register_instrument(custom)
        assert get_instrument("AUDNZD") is custom
