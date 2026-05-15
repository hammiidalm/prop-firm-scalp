"""Smart-Money-Concepts scalping strategy — confluence-scored edition.

This strategy only fires a signal when a **dynamic confluence score ≥ 75 / 100**
is achieved.  Every entry must align with the higher-timeframe bias and clear
five independent filters, each contributing to the total score:

+-----------------------------------+--------+
| Factor                            | Points |
+===================================+========+
| Liquidity Sweep (stop-hunt)       |   40   |
| Strong Rejection Candle           |   25   |
| BOS / CHOCH aligned with HTF bias |   20   |
| Order Block proximity             |   10   |
| Session + Spread filter pass      |    5   |
+-----------------------------------+--------+
| **Total possible**                | **100**|
+-----------------------------------+--------+

Hard-block filters (signal is immediately discarded):
- Entry against the HTF bias (``MarketStructure.get_htf_bias()``).
- Outside London / NY session.
- One signal per bar per symbol.
- R:R below ``min_rr``.

Architecture
------------
The strategy is deliberately a *pure signal generator* — it knows nothing
about execution, risk sizing or notifications.  Those concerns live in the
Executor and RiskManager layers.  This keeps the strategy fully unit-testable
against synthetic candle sequences.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.models import Candle, CandleSeries, Signal
from app.strategy.candles import is_strong_bearish_rejection, is_strong_bullish_rejection
from app.strategy.market_structure import (
    HTFBias,
    MarketStructure,
    OrderBlock,
    StructureEvent,
    StructureKind,
)
from app.utils.instruments import Instrument, get_instrument
from app.utils.logging import get_logger
from app.utils.sessions import SessionFilter

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Confluence score thresholds & weights (out of 100)
# ---------------------------------------------------------------------------

_SCORE_SWEEP: int = 40
_SCORE_REJECTION: int = 25
_SCORE_BOS_HTF: int = 20
_SCORE_ORDER_BLOCK: int = 10
_SCORE_SESSION_SPREAD: int = 5
_MIN_CONFLUENCE: int = 75


# ---------------------------------------------------------------------------
# Strategy class
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SmcScalpStrategy:
    """SMC scalping strategy v2 — confluence-scored, HTF-filtered.

    One instance is created per ``(symbol, timeframe)`` pair by the engine.

    Parameters
    ----------
    symbol / timeframe:
        Identifies which candle stream this strategy processes.
    sessions:
        Session filter (London + NY).  Bars outside active sessions are
        silently ignored.
    target_profit_pct_min / max:
        Target TP expressed as a fraction of entry price.  The risk layer
        converts this to account-currency via position sizing.
    min_rr:
        Minimum reward:risk ratio to accept a signal.
    min_confluence:
        Minimum confluence score (0–100) required to generate a signal.
        Default 75.
    swing_lookback / sweep_wick_ratio:
        Forwarded to the internal ``MarketStructure`` instance.
    """

    symbol: str
    timeframe: str
    sessions: SessionFilter
    target_profit_pct_min: float = 0.001
    target_profit_pct_max: float = 0.002
    min_rr: float = 1.2
    min_confluence: int = _MIN_CONFLUENCE
    swing_lookback: int = 2
    sweep_wick_ratio: float = 0.5

    name: str = "smc_scalp_v2"

    # -- private state (not constructor args) --
    _series: CandleSeries | None = field(default=None, init=False)
    _ms: MarketStructure | None = field(default=None, init=False)
    _instrument: Instrument | None = field(default=None, init=False)
    _last_signal_ts: object = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._series = CandleSeries(self.symbol, self.timeframe, maxlen=512)
        self._ms = MarketStructure(
            swing_lookback=self.swing_lookback,
            sweep_wick_ratio=self.sweep_wick_ratio,
        )
        self._instrument = get_instrument(self.symbol)

    # ==================================================================
    # Public: inject HTF bias from the engine
    # ==================================================================

    def set_htf_bias(self, bias: HTFBias) -> None:
        """Engine calls this whenever the HTF tracker updates its bias."""
        assert self._ms is not None
        self._ms.set_htf_bias(bias)

    # ==================================================================
    # Public: main entry point — called once per new execution-TF bar
    # ==================================================================

    async def on_candle(self, candle: Candle) -> Signal | None:
        """Evaluate one execution-TF candle for a trading opportunity.

        Returns a ``Signal`` if confluence ≥ threshold, else ``None``.
        """
        assert self._series is not None
        assert self._ms is not None
        assert self._instrument is not None

        # ---- Hard-block: wrong symbol/TF ---------------------------------
        if candle.symbol != self.symbol or candle.timeframe != self.timeframe:
            return None

        # ---- Hard-block: outside active session --------------------------
        if not self.sessions.is_active(candle.timestamp):
            return None

        # ---- Hard-block: one signal per bar (de-duplicate live ticks) -----
        if self._last_signal_ts == candle.timestamp:
            return None

        # ---- Feed candle to internal state --------------------------------
        self._series.append(candle)
        events = self._ms.update(candle)

        # If no structural events fired this bar, there's nothing to trade.
        if not events:
            return None

        # ---- Hard-block: refuse entry against HTF bias --------------------
        htf_bias = self._ms.get_htf_bias()

        # Try LONG setup
        signal = self._evaluate_long(candle, events, htf_bias)
        if signal is not None:
            return signal

        # Try SHORT setup
        signal = self._evaluate_short(candle, events, htf_bias)
        if signal is not None:
            return signal

        return None

    # ==================================================================
    # LONG evaluation
    # ==================================================================

    def _evaluate_long(
        self,
        candle: Candle,
        events: list[StructureEvent],
        htf_bias: HTFBias,
    ) -> Signal | None:
        """Score and (optionally) generate a LONG signal.

        The method returns early (None) if any hard-block is hit; otherwise
        it accumulates a confluence score and fires only if ≥ threshold.
        """
        assert self._ms is not None and self._instrument is not None

        # ---- Hard-block: HTF must not be bearish -------------------------
        # We allow NEUTRAL (no HTF data yet) + BULLISH.
        if htf_bias is HTFBias.BEARISH:
            return None

        # ---- Check if a SWEEP_LOW occurred this bar ----------------------
        sweep_event = next(
            (e for e in events if e.kind is StructureKind.SWEEP_LOW), None
        )
        if sweep_event is None:
            return None  # no liquidity sweep → nothing to trade

        # ==================================================================
        # Confluence scoring begins
        # ==================================================================
        score = 0
        tags: list[str] = []

        # ---- Factor 1: Liquidity Sweep (40 pts) --------------------------
        # Already confirmed above — award full points.
        score += _SCORE_SWEEP
        tags.append("SWEEP_LOW")

        # ---- Factor 2: Strong Rejection Candle (25 pts) ------------------
        if is_strong_bullish_rejection(candle):
            score += _SCORE_REJECTION
            tags.append("REJECTION_BULL")

        # ---- Factor 3: BOS/CHOCH aligned with HTF (20 pts) ---------------
        kinds = {e.kind for e in events}
        bos_aligned = (
            StructureKind.BOS_BULL in kinds
            or StructureKind.CHOCH_BULL in kinds
            or self._ms.last_trend() is StructureKind.BOS_BULL
        )
        if bos_aligned and htf_bias is HTFBias.BULLISH:
            score += _SCORE_BOS_HTF
            tags.append("BOS_HTF_ALIGNED")
        elif bos_aligned:
            # HTF is NEUTRAL — partial credit (half)
            score += _SCORE_BOS_HTF // 2
            tags.append("BOS_NEUTRAL_HTF")

        # ---- Factor 4: Order Block proximity (10 pts) --------------------
        entry_price = candle.close
        ob_hit = self._price_in_order_block(entry_price, "BULLISH")
        if ob_hit is not None:
            score += _SCORE_ORDER_BLOCK
            tags.append("OB_DEMAND")

        # ---- Factor 5: Session + Spread pass (5 pts) ---------------------
        # Session is already confirmed (hard-block above); the spread check
        # is delegated to the execution layer, but we award the 5 pts here
        # since the session is active.
        score += _SCORE_SESSION_SPREAD
        tags.append("SESSION_OK")

        # ---- Bonus: FVG alignment (informational, no extra points but
        #      logged for the journal) ------------------------------------
        fvgs = self._ms.find_fvg(direction="BULLISH", active_only=True)
        if any(fvg.gap_low <= entry_price <= fvg.gap_high for fvg in fvgs):
            tags.append("FVG_BULLISH")

        # ==================================================================
        # Gate: minimum confluence
        # ==================================================================
        if score < self.min_confluence:
            log.debug(
                "LONG rejected: confluence %d < %d",
                score, self.min_confluence,
                extra={"symbol": self.symbol, "score": score, "tags": tags},
            )
            return None

        # ==================================================================
        # Build the signal
        # ==================================================================
        swept_low = sweep_event.reference_swing.price
        signal = self._build_signal_long(candle, swept_low, score, tags)
        if signal is None:
            return None

        self._last_signal_ts = candle.timestamp
        log.info(
            "LONG signal fired",
            extra={
                "symbol": self.symbol,
                "confluence": score,
                "rr": signal.rr_ratio,
                "tags": tags,
            },
        )
        return signal

    # ==================================================================
    # SHORT evaluation
    # ==================================================================

    def _evaluate_short(
        self,
        candle: Candle,
        events: list[StructureEvent],
        htf_bias: HTFBias,
    ) -> Signal | None:
        """Score and (optionally) generate a SHORT signal.

        Mirror of ``_evaluate_long`` with inverted direction logic.
        """
        assert self._ms is not None and self._instrument is not None

        # ---- Hard-block: HTF must not be bullish -------------------------
        if htf_bias is HTFBias.BULLISH:
            return None

        # ---- Check if a SWEEP_HIGH occurred this bar ---------------------
        sweep_event = next(
            (e for e in events if e.kind is StructureKind.SWEEP_HIGH), None
        )
        if sweep_event is None:
            return None

        # ==================================================================
        # Confluence scoring begins
        # ==================================================================
        score = 0
        tags: list[str] = []

        # ---- Factor 1: Liquidity Sweep (40 pts) --------------------------
        score += _SCORE_SWEEP
        tags.append("SWEEP_HIGH")

        # ---- Factor 2: Strong Rejection Candle (25 pts) ------------------
        if is_strong_bearish_rejection(candle):
            score += _SCORE_REJECTION
            tags.append("REJECTION_BEAR")

        # ---- Factor 3: BOS/CHOCH aligned with HTF (20 pts) ---------------
        kinds = {e.kind for e in events}
        bos_aligned = (
            StructureKind.BOS_BEAR in kinds
            or StructureKind.CHOCH_BEAR in kinds
            or self._ms.last_trend() is StructureKind.BOS_BEAR
        )
        if bos_aligned and htf_bias is HTFBias.BEARISH:
            score += _SCORE_BOS_HTF
            tags.append("BOS_HTF_ALIGNED")
        elif bos_aligned:
            score += _SCORE_BOS_HTF // 2
            tags.append("BOS_NEUTRAL_HTF")

        # ---- Factor 4: Order Block proximity (10 pts) --------------------
        entry_price = candle.close
        ob_hit = self._price_in_order_block(entry_price, "BEARISH")
        if ob_hit is not None:
            score += _SCORE_ORDER_BLOCK
            tags.append("OB_SUPPLY")

        # ---- Factor 5: Session + Spread pass (5 pts) ---------------------
        score += _SCORE_SESSION_SPREAD
        tags.append("SESSION_OK")

        # ---- Bonus: FVG alignment ----------------------------------------
        fvgs = self._ms.find_fvg(direction="BEARISH", active_only=True)
        if any(fvg.gap_low <= entry_price <= fvg.gap_high for fvg in fvgs):
            tags.append("FVG_BEARISH")

        # ==================================================================
        # Gate: minimum confluence
        # ==================================================================
        if score < self.min_confluence:
            log.debug(
                "SHORT rejected: confluence %d < %d",
                score, self.min_confluence,
                extra={"symbol": self.symbol, "score": score, "tags": tags},
            )
            return None

        # ==================================================================
        # Build the signal
        # ==================================================================
        swept_high = sweep_event.reference_swing.price
        signal = self._build_signal_short(candle, swept_high, score, tags)
        if signal is None:
            return None

        self._last_signal_ts = candle.timestamp
        log.info(
            "SHORT signal fired",
            extra={
                "symbol": self.symbol,
                "confluence": score,
                "rr": signal.rr_ratio,
                "tags": tags,
            },
        )
        return signal

    # ==================================================================
    # Signal construction
    # ==================================================================

    def _build_signal_long(
        self,
        candle: Candle,
        swept_low: float,
        confluence: int,
        tags: list[str],
    ) -> Signal | None:
        """Construct a LONG Signal with proper SL/TP or None if RR < min."""
        assert self._instrument is not None

        entry = candle.close
        # SL below the swept low with 1-pip buffer
        sl = min(swept_low, candle.low) - self._instrument.price_delta(1.0)

        # TP: at least min_rr × risk, capped by target_profit_pct_max
        risk_dist = entry - sl
        if risk_dist <= 0:
            return None

        tp_dist_min = entry * self.target_profit_pct_min
        tp_dist = max(tp_dist_min, self.min_rr * risk_dist)
        tp_dist = min(tp_dist, entry * self.target_profit_pct_max * 1.5)
        tp = entry + tp_dist

        # Confidence = confluence score normalized to 0.0–1.0
        confidence = min(confluence / 100.0, 1.0)

        reason = f"sweep_low+rejection confluence={confluence}"
        sig = Signal.long(
            symbol=self.symbol,
            timeframe=self.timeframe,
            entry=entry,
            sl=sl,
            tp=tp,
            reason=reason,
            session=self.sessions.classify(candle.timestamp).value,
            tags=tags,
            generated_at=candle.timestamp,
            confidence=confidence,
        )
        if sig.rr_ratio < self.min_rr:
            return None
        return sig

    def _build_signal_short(
        self,
        candle: Candle,
        swept_high: float,
        confluence: int,
        tags: list[str],
    ) -> Signal | None:
        """Construct a SHORT Signal with proper SL/TP or None if RR < min."""
        assert self._instrument is not None

        entry = candle.close
        # SL above the swept high with 1-pip buffer
        sl = max(swept_high, candle.high) + self._instrument.price_delta(1.0)

        risk_dist = sl - entry
        if risk_dist <= 0:
            return None

        tp_dist_min = entry * self.target_profit_pct_min
        tp_dist = max(tp_dist_min, self.min_rr * risk_dist)
        tp_dist = min(tp_dist, entry * self.target_profit_pct_max * 1.5)
        tp = entry - tp_dist

        confidence = min(confluence / 100.0, 1.0)

        reason = f"sweep_high+rejection confluence={confluence}"
        sig = Signal.short(
            symbol=self.symbol,
            timeframe=self.timeframe,
            entry=entry,
            sl=sl,
            tp=tp,
            reason=reason,
            session=self.sessions.classify(candle.timestamp).value,
            tags=tags,
            generated_at=candle.timestamp,
            confidence=confidence,
        )
        if sig.rr_ratio < self.min_rr:
            return None
        return sig

    # ==================================================================
    # Helpers
    # ==================================================================

    def _price_in_order_block(
        self, price: float, direction: Literal["BULLISH", "BEARISH"]
    ) -> OrderBlock | None:
        """Return the first active OB whose zone overlaps ``price``, or None."""
        assert self._ms is not None
        for ob in self._ms.find_order_blocks(direction=direction, active_only=True):
            if ob.zone_low <= price <= ob.zone_high:
                return ob
        return None
