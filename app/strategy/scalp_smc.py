"""Smart-Money-Concepts scalping strategy.

Entry rules (LONG, mirror for SHORT):
    1. Liquidity sweep below a recent swing low.
    2. The sweep candle (or the next one) is a strong bullish rejection.
    3. A minor bullish BOS confirms the structural shift on the same/next bar.
    4. Entry is taken at market on confirmation; stop just below the sweep
       low; take-profit sized for a target equity move of
       ``target_profit_pct_min..max`` of account balance.
    5. RR must be at least ``min_rr`` to avoid sub-optimal scalps.

Filters:
    * Only inside London/NY sessions.
    * Spread must be inside the per-symbol cap (handled in execution layer).
    * One signal per bar; the engine throttles repeats.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import Candle, CandleSeries, Signal
from app.strategy.candles import is_strong_bearish_rejection, is_strong_bullish_rejection
from app.strategy.market_structure import MarketStructure, StructureKind
from app.utils.instruments import Instrument, get_instrument
from app.utils.logging import get_logger
from app.utils.sessions import SessionFilter

log = get_logger(__name__)


@dataclass(slots=True)
class SmcScalpStrategy:
    """SMC scalping strategy. One instance per ``(symbol, timeframe)``."""

    symbol: str
    timeframe: str
    sessions: SessionFilter
    target_profit_pct_min: float = 0.001
    target_profit_pct_max: float = 0.002
    min_rr: float = 1.2
    swing_lookback: int = 2
    sweep_wick_ratio: float = 0.5

    name: str = "smc_scalp"

    _series: CandleSeries | None = None
    _ms: MarketStructure | None = None
    _instrument: Instrument | None = None
    _last_signal_ts: object = None  # datetime, but typed loose to placate mypy on dataclass default

    def __post_init__(self) -> None:
        self._series = CandleSeries(self.symbol, self.timeframe, maxlen=512)
        self._ms = MarketStructure(
            swing_lookback=self.swing_lookback,
            sweep_wick_ratio=self.sweep_wick_ratio,
        )
        self._instrument = get_instrument(self.symbol)

    async def on_candle(self, candle: Candle) -> Signal | None:
        assert self._series is not None and self._ms is not None and self._instrument is not None

        if candle.symbol != self.symbol or candle.timeframe != self.timeframe:
            return None
        if not self.sessions.is_active(candle.timestamp):
            return None
        if self._last_signal_ts == candle.timestamp:
            return None

        self._series.append(candle)
        events = self._ms.update(candle)
        if not events:
            return None

        kinds = {e.kind for e in events}
        sweep_low = next((e for e in events if e.kind is StructureKind.SWEEP_LOW), None)
        sweep_high = next((e for e in events if e.kind is StructureKind.SWEEP_HIGH), None)
        bos_bull = StructureKind.BOS_BULL in kinds or StructureKind.CHOCH_BULL in kinds
        bos_bear = StructureKind.BOS_BEAR in kinds or StructureKind.CHOCH_BEAR in kinds

        # ---- LONG: sweep_low + bullish rejection + structural confirmation
        if sweep_low and is_strong_bullish_rejection(candle):
            # Same-bar BOS is rare on M1; we accept the sweep+rejection alone
            # if the prior trend was already bullish, otherwise we require a
            # CHOCH_BULL.
            trend_ok = bos_bull or self._ms.last_trend() is StructureKind.BOS_BULL
            if trend_ok:
                signal = self._build_signal_long(candle, sweep_low.reference_swing.price)
                if signal:
                    self._last_signal_ts = candle.timestamp
                    log.info("LONG signal", extra={"symbol": self.symbol, "rr": signal.rr_ratio})
                    return signal

        # ---- SHORT: mirror
        if sweep_high and is_strong_bearish_rejection(candle):
            trend_ok = bos_bear or self._ms.last_trend() is StructureKind.BOS_BEAR
            if trend_ok:
                signal = self._build_signal_short(candle, sweep_high.reference_swing.price)
                if signal:
                    self._last_signal_ts = candle.timestamp
                    log.info("SHORT signal", extra={"symbol": self.symbol, "rr": signal.rr_ratio})
                    return signal

        return None

    # ---- helpers --------------------------------------------------------
    def _build_signal_long(self, candle: Candle, swept_low: float) -> Signal | None:
        assert self._instrument is not None
        entry = candle.close
        # SL just below the swept low - small buffer of 1 pip.
        sl = min(swept_low, candle.low) - self._instrument.price_delta(1.0)
        # TP from configured equity-move target. We map the % move on
        # *price* (not equity) since position size is solved by the risk
        # layer to satisfy the equity risk budget.
        tp_dist_min = entry * self.target_profit_pct_min
        tp_dist_max = entry * self.target_profit_pct_max
        risk_dist = entry - sl
        if risk_dist <= 0:
            return None
        # Aim for the larger of (configured target) and (min_rr * risk).
        tp_dist = max(tp_dist_min, self.min_rr * risk_dist)
        tp_dist = min(tp_dist, tp_dist_max * 1.5)  # don't overshoot too far
        tp = entry + tp_dist
        sig = Signal.long(
            symbol=self.symbol,
            timeframe=self.timeframe,
            entry=entry,
            sl=sl,
            tp=tp,
            reason="sweep_low + bullish_rejection",
            session=self.sessions.classify(candle.timestamp).value,
            tags=["SWEEP_LOW", "REJECTION", "BOS_BULL"],
            generated_at=candle.timestamp,
            confidence=0.6,
        )
        if sig.rr_ratio < self.min_rr:
            return None
        return sig

    def _build_signal_short(self, candle: Candle, swept_high: float) -> Signal | None:
        assert self._instrument is not None
        entry = candle.close
        sl = max(swept_high, candle.high) + self._instrument.price_delta(1.0)
        tp_dist_min = entry * self.target_profit_pct_min
        tp_dist_max = entry * self.target_profit_pct_max
        risk_dist = sl - entry
        if risk_dist <= 0:
            return None
        tp_dist = max(tp_dist_min, self.min_rr * risk_dist)
        tp_dist = min(tp_dist, tp_dist_max * 1.5)
        tp = entry - tp_dist
        sig = Signal.short(
            symbol=self.symbol,
            timeframe=self.timeframe,
            entry=entry,
            sl=sl,
            tp=tp,
            reason="sweep_high + bearish_rejection",
            session=self.sessions.classify(candle.timestamp).value,
            tags=["SWEEP_HIGH", "REJECTION", "BOS_BEAR"],
            generated_at=candle.timestamp,
            confidence=0.6,
        )
        if sig.rr_ratio < self.min_rr:
            return None
        return sig
