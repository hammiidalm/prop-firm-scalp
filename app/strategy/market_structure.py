"""Smart-money market-structure primitives.

Concepts implemented
--------------------
* **Adaptive swing detection** — local extrema filtered by ATR-scaled
  prominence so noise candles are rejected in high-volatility regimes.
* **Break of Structure (BOS)** — close beyond the most recent swing in
  the direction of the prevailing trend.
* **Change of Character (CHOCH)** — close beyond the *opposing* swing,
  signalling a potential trend reversal.
* **Liquidity sweep** — wick through a swing point with a close back
  inside the prior range (stop-hunt pattern).
* **Order Block (OB)** — the last opposing candle before a displacement
  move that confirmed structure. Represents an institutional supply/demand
  zone.
* **Fair Value Gap (FVG)** — a three-candle price imbalance (gap between
  bar[0] extreme and bar[2] extreme not covered by bar[1]).
* **Higher-Timeframe Bias (HTF)** — directional trend derived from a
  separate ``HTFStructure`` tracker running on a higher timeframe. The
  execution-TF strategy receives the bias via ``set_htf_bias()``.

Architecture
------------
``HTFStructure`` — lightweight standalone tracker (no OB/FVG) intended
    to be fed higher-timeframe bars. Its ``bias`` property is polled by
    the execution-TF ``MarketStructure`` or strategy.

``MarketStructure`` — full stateful tracker fed execution-TF bars. All
    public methods are pure accessors except ``update()`` which mutates
    state and returns a list of ``StructureEvent`` objects.

All public types are frozen dataclasses so they can be safely stored,
logged, and compared.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal

from app.models import Candle

# ---------------------------------------------------------------------------
# Public enumerations
# ---------------------------------------------------------------------------


class SwingType(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"


class StructureKind(str, Enum):
    BOS_BULL = "BOS_BULL"
    BOS_BEAR = "BOS_BEAR"
    CHOCH_BULL = "CHOCH_BULL"
    CHOCH_BEAR = "CHOCH_BEAR"
    SWEEP_HIGH = "SWEEP_HIGH"   # liquidity grab above swing high → bearish
    SWEEP_LOW = "SWEEP_LOW"     # liquidity grab below swing low  → bullish


class HTFBias(str, Enum):
    """Directional bias derived from the higher timeframe."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


# ---------------------------------------------------------------------------
# Immutable value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SwingPoint:
    """A confirmed swing high or low.

    ``prominence`` is the absolute price distance between the pivot and
    the next-closest neighbour in its detection window.  It is normalised
    against ATR by the caller when ``adaptive_swing`` is enabled.
    """

    type: SwingType
    price: float
    timestamp: datetime
    index: int
    prominence: float = 0.0


@dataclass(frozen=True, slots=True)
class StructureEvent:
    """A discrete structural event emitted by ``MarketStructure.update()``."""

    kind: StructureKind
    candle: Candle
    reference_swing: SwingPoint
    note: str = ""


@dataclass(frozen=True, slots=True)
class OrderBlock:
    """An institutional supply/demand zone.

    Defined as the *last opposing candle* before a displacement move that
    broke structure.  The zone is the body of that candle
    (``zone_low`` … ``zone_high``).

    Attributes
    ----------
    direction:
        ``"BULLISH"`` — demand zone (price likely to bounce up from it).
        ``"BEARISH"`` — supply zone (price likely to reject from it).
    displacement_range:
        Body size of the displacement candle that validated this OB.
    mitigated:
        Set to ``True`` once price trades back into the OB zone.
    """

    direction: Literal["BULLISH", "BEARISH"]
    candle: Candle
    zone_high: float
    zone_low: float
    timestamp: datetime
    displacement_range: float
    mitigated: bool = False


@dataclass(frozen=True, slots=True)
class FairValueGap:
    """A three-candle price imbalance (inefficiency).

    Bullish FVG: ``bar[0].high < bar[2].low``  — gap to the upside.
    Bearish FVG: ``bar[0].low  > bar[2].high`` — gap to the downside.

    ``bar[1]`` is the displacement candle whose timestamp is stored.

    Attributes
    ----------
    gap_high / gap_low:
        Boundaries of the unfilled price range.
    filled:
        Set to ``True`` once price trades back through the gap entirely.
    """

    direction: Literal["BULLISH", "BEARISH"]
    gap_high: float
    gap_low: float
    timestamp: datetime
    filled: bool = False

    @property
    def size(self) -> float:
        """Absolute size of the gap in price units."""
        return self.gap_high - self.gap_low

    @property
    def midpoint(self) -> float:
        """50 % equilibrium of the gap."""
        return (self.gap_high + self.gap_low) / 2.0



# ---------------------------------------------------------------------------
# HTFStructure — standalone higher-timeframe bias tracker
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HTFStructure:
    """Lightweight stateful tracker for a *single* higher timeframe.

    Feed it bars from the HTF (e.g. M15 when trading M1/M5) and read
    ``.bias`` to get the current directional context.  Internally it runs
    the same swing/BOS/CHOCH logic as ``MarketStructure`` but without the
    OB/FVG machinery, keeping CPU cost minimal.

    Parameters
    ----------
    swing_lookback:
        Bars on each side for local-extremum detection.
    sweep_wick_ratio:
        Minimum wick-rejection ratio for sweep detection.

    Usage example
    -------------
    ::

        htf = HTFStructure(swing_lookback=3)
        for bar in m15_bars:
            htf.update(bar)

        ltf_ms.set_htf_bias(htf.bias)
    """

    swing_lookback: int = 3
    sweep_wick_ratio: float = 0.5

    _candles: deque[Candle] = field(
        default_factory=lambda: deque(maxlen=256), init=False
    )
    _swings: deque[SwingPoint] = field(
        default_factory=lambda: deque(maxlen=40), init=False
    )
    _last_bos: StructureKind | None = field(default=None, init=False)

    # ------------------------------------------------------------------
    def update(self, candle: Candle) -> list[StructureEvent]:
        """Ingest one HTF candle.  Returns structural events (BOS/CHOCH/sweep)."""
        self._candles.append(candle)
        events: list[StructureEvent] = []

        self._detect_swings_simple()

        last_high = self._latest_swing(SwingType.HIGH)
        last_low = self._latest_swing(SwingType.LOW)

        # Sweeps
        if last_high is not None and self._is_sweep_high(candle, last_high.price):
            events.append(StructureEvent(
                kind=StructureKind.SWEEP_HIGH,
                candle=candle,
                reference_swing=last_high,
                note=f"HTF wick above {last_high.price:.5f}",
            ))
        if last_low is not None and self._is_sweep_low(candle, last_low.price):
            events.append(StructureEvent(
                kind=StructureKind.SWEEP_LOW,
                candle=candle,
                reference_swing=last_low,
                note=f"HTF wick below {last_low.price:.5f}",
            ))

        # BOS / CHOCH
        if last_high is not None and candle.close > last_high.price:
            kind = (
                StructureKind.CHOCH_BULL
                if self._last_bos is StructureKind.BOS_BEAR
                else StructureKind.BOS_BULL
            )
            events.append(StructureEvent(kind=kind, candle=candle,
                                         reference_swing=last_high))
            self._last_bos = StructureKind.BOS_BULL
        elif last_low is not None and candle.close < last_low.price:
            kind = (
                StructureKind.CHOCH_BEAR
                if self._last_bos is StructureKind.BOS_BULL
                else StructureKind.BOS_BEAR
            )
            events.append(StructureEvent(kind=kind, candle=candle,
                                         reference_swing=last_low))
            self._last_bos = StructureKind.BOS_BEAR

        return events

    @property
    def bias(self) -> HTFBias:
        """Current higher-timeframe directional bias."""
        if self._last_bos in (StructureKind.BOS_BULL, StructureKind.CHOCH_BULL):
            return HTFBias.BULLISH
        if self._last_bos in (StructureKind.BOS_BEAR, StructureKind.CHOCH_BEAR):
            return HTFBias.BEARISH
        return HTFBias.NEUTRAL

    def latest_swing_high(self) -> SwingPoint | None:
        return self._latest_swing(SwingType.HIGH)

    def latest_swing_low(self) -> SwingPoint | None:
        return self._latest_swing(SwingType.LOW)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _detect_swings_simple(self) -> None:
        n = len(self._candles)
        k = self.swing_lookback
        if n < 2 * k + 1:
            return
        pivot_idx = n - k - 1
        pivot = self._candles[pivot_idx]
        window = list(self._candles)[pivot_idx - k: pivot_idx + k + 1]
        if pivot.high == max(c.high for c in window):
            self._add_swing(SwingPoint(
                SwingType.HIGH, pivot.high, pivot.timestamp, pivot_idx))
        if pivot.low == min(c.low for c in window):
            self._add_swing(SwingPoint(
                SwingType.LOW, pivot.low, pivot.timestamp, pivot_idx))

    def _add_swing(self, swing: SwingPoint) -> None:
        if (self._swings
                and self._swings[-1].timestamp == swing.timestamp
                and self._swings[-1].type == swing.type):
            return
        self._swings.append(swing)

    def _latest_swing(self, kind: SwingType) -> SwingPoint | None:
        for s in reversed(self._swings):
            if s.type == kind:
                return s
        return None

    def _is_sweep_high(self, candle: Candle, swing_high: float) -> bool:
        if candle.high <= swing_high or candle.close >= swing_high:
            return False
        if candle.range == 0:
            return False
        wick = candle.high - max(candle.close, candle.open)
        return wick / candle.range >= self.sweep_wick_ratio

    def _is_sweep_low(self, candle: Candle, swing_low: float) -> bool:
        if candle.low >= swing_low or candle.close <= swing_low:
            return False
        if candle.range == 0:
            return False
        wick = min(candle.close, candle.open) - candle.low
        return wick / candle.range >= self.sweep_wick_ratio



# ---------------------------------------------------------------------------
# MarketStructure — full execution-TF tracker
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MarketStructure:
    """Full stateful market-structure tracker for the execution timeframe.

    Feed execution-TF candles via ``update()`` and:

    * Read structural events (sweeps, BOS, CHOCH) in the return value.
    * Query ``find_order_blocks()`` and ``find_fvg()`` for premium/discount
      zones to use as entry refinements.
    * Call ``calculate_confluence_score()`` to get a 0–1 grade for any
      candidate entry price.
    * Inject higher-timeframe bias with ``set_htf_bias()`` (feed it from a
      sibling ``HTFStructure`` instance running on M15/H1).

    Parameters
    ----------
    swing_lookback:
        Minimum bars on each side for local-extremum detection.
    sweep_wick_ratio:
        Minimum (rejection wick / candle range) to qualify as a sweep.
    history:
        Maximum number of swing points stored (rolling).
    adaptive_swing:
        When ``True``, a prominence filter (ATR-based) is applied so that
        noisy micro-swings are suppressed in high-volatility regimes.
    prominence_atr_mult:
        Minimum swing prominence expressed as a multiple of ATR.  Only
        relevant when ``adaptive_swing=True``.  Lower values → more
        sensitive; higher values → only major pivots qualify.
    ob_displacement_mult:
        Minimum body size of the BOS/displacement candle (as ATR multiple)
        needed to validate an order block.
    min_fvg_atr_mult:
        Minimum FVG size (gap / ATR) to store.  Gaps smaller than this
        are considered noise and discarded.
    """

    swing_lookback: int = 2
    sweep_wick_ratio: float = 0.5
    history: int = 50
    adaptive_swing: bool = True
    prominence_atr_mult: float = 0.3
    ob_displacement_mult: float = 1.5
    min_fvg_atr_mult: float = 0.2

    # ------------------------------------------------------------------
    # Private state
    # ------------------------------------------------------------------
    _candles: deque[Candle] = field(
        default_factory=lambda: deque(maxlen=512), init=False
    )
    _swings: deque[SwingPoint] = field(
        default_factory=lambda: deque(maxlen=50), init=False
    )
    _last_bos: StructureKind | None = field(default=None, init=False)
    _order_blocks: deque[OrderBlock] = field(
        default_factory=lambda: deque(maxlen=30), init=False
    )
    _fvgs: deque[FairValueGap] = field(
        default_factory=lambda: deque(maxlen=30), init=False
    )
    # HTF bias injected externally (decoupled from LTF state)
    _htf_bias: HTFBias = field(default=HTFBias.NEUTRAL, init=False)

    # ==================================================================
    # Primary public interface
    # ==================================================================

    def update(self, candle: Candle) -> list[StructureEvent]:
        """Ingest one execution-TF candle; return all triggered events.

        Processing order (important for accuracy):
        1. Append candle and detect adaptive swings on the *confirmed*
           pivot (``swing_lookback`` bars old).
        2. Check for liquidity sweeps against the most recent swings.
        3. Check for BOS / CHOCH.  On a structural break, immediately
           scan for the preceding order block.
        4. Scan the three most recent bars for a Fair Value Gap.
        5. Mitigate (mark as used) any OB or FVG that price has revisited.
        """
        self._candles.append(candle)
        events: list[StructureEvent] = []

        # --- 1. Swing detection (adaptive) ---
        self._detect_swings()

        last_high = self._latest_swing(SwingType.HIGH)
        last_low = self._latest_swing(SwingType.LOW)

        # --- 2. Liquidity sweeps ---
        if last_high is not None and self._is_sweep_high(candle, last_high.price):
            events.append(StructureEvent(
                kind=StructureKind.SWEEP_HIGH,
                candle=candle,
                reference_swing=last_high,
                note=f"wick above {last_high.price:.5f} closed back below",
            ))
        if last_low is not None and self._is_sweep_low(candle, last_low.price):
            events.append(StructureEvent(
                kind=StructureKind.SWEEP_LOW,
                candle=candle,
                reference_swing=last_low,
                note=f"wick below {last_low.price:.5f} closed back above",
            ))

        # --- 3. BOS / CHOCH ---
        if last_high is not None and candle.close > last_high.price:
            kind = (
                StructureKind.CHOCH_BULL
                if self._last_bos is StructureKind.BOS_BEAR
                else StructureKind.BOS_BULL
            )
            events.append(StructureEvent(
                kind=kind, candle=candle, reference_swing=last_high
            ))
            self._last_bos = StructureKind.BOS_BULL
            self._detect_order_block(candle, "BULLISH")
        elif last_low is not None and candle.close < last_low.price:
            kind = (
                StructureKind.CHOCH_BEAR
                if self._last_bos is StructureKind.BOS_BULL
                else StructureKind.BOS_BEAR
            )
            events.append(StructureEvent(
                kind=kind, candle=candle, reference_swing=last_low
            ))
            self._last_bos = StructureKind.BOS_BEAR
            self._detect_order_block(candle, "BEARISH")

        # --- 4. FVG detection ---
        self._detect_fvg()

        # --- 5. Mitigation ---
        self._mitigate_obs(candle)
        self._fill_fvgs(candle)

        return events

    # ==================================================================
    # HTF bias
    # ==================================================================

    def get_htf_bias(self) -> HTFBias:
        """Return the higher-timeframe directional bias.

        Returns ``HTFBias.NEUTRAL`` until ``set_htf_bias()`` is called.
        Integrate with ``HTFStructure``::

            htf = HTFStructure(swing_lookback=3)
            ltf = MarketStructure()
            ...
            # whenever a new M15 bar closes:
            htf.update(m15_bar)
            ltf.set_htf_bias(htf.bias)
        """
        return self._htf_bias

    def set_htf_bias(self, bias: HTFBias) -> None:
        """Inject the higher-timeframe bias computed by ``HTFStructure``."""
        self._htf_bias = bias

    # ==================================================================
    # Order blocks
    # ==================================================================

    def find_order_blocks(
        self,
        direction: Literal["BULLISH", "BEARISH"] | None = None,
        *,
        active_only: bool = True,
    ) -> list[OrderBlock]:
        """Return stored order blocks, newest first.

        Parameters
        ----------
        direction:
            Filter to a single direction.  ``None`` returns both.
        active_only:
            When ``True`` (default), exclude mitigated blocks.
        """
        result: list[OrderBlock] = []
        for ob in reversed(self._order_blocks):
            if active_only and ob.mitigated:
                continue
            if direction is not None and ob.direction != direction:
                continue
            result.append(ob)
        return result

    # ==================================================================
    # Fair value gaps
    # ==================================================================

    def find_fvg(
        self,
        direction: Literal["BULLISH", "BEARISH"] | None = None,
        *,
        active_only: bool = True,
    ) -> list[FairValueGap]:
        """Return stored fair value gaps, newest first.

        Parameters
        ----------
        direction:
            Filter to a single direction.  ``None`` returns both.
        active_only:
            When ``True`` (default), exclude filled gaps.
        """
        result: list[FairValueGap] = []
        for fvg in reversed(self._fvgs):
            if active_only and fvg.filled:
                continue
            if direction is not None and fvg.direction != direction:
                continue
            result.append(fvg)
        return result

    # ==================================================================
    # Confluence scoring
    # ==================================================================

    def calculate_confluence_score(
        self,
        price: float,
        direction: Literal["BULLISH", "BEARISH"],
    ) -> float:
        """Score a candidate entry at ``price`` from 0.0 to 1.0.

        Five independent factors, each capped at the weight shown:

        +---------------------------------+--------+
        | Factor                          | Weight |
        +=================================+========+
        | HTF bias alignment              |  0.25  |
        | Active OB overlapping ``price`` |  0.25  |
        | Active FVG overlapping ``price``|  0.20  |
        | LTF structure alignment         |  0.20  |
        | Swing proximity (≤2×ATR)        |  0.10  |
        +---------------------------------+--------+

        Total possible: 1.00.  A score ≥ 0.60 is considered strong
        confluence; ≥ 0.80 is exceptional.
        """
        score = 0.0

        # 1 — HTF bias
        if direction == "BULLISH" and self._htf_bias is HTFBias.BULLISH:
            score += 0.25
        elif direction == "BEARISH" and self._htf_bias is HTFBias.BEARISH:
            score += 0.25

        # 2 — Order block at price
        for ob in self.find_order_blocks(direction=direction, active_only=True):
            if ob.zone_low <= price <= ob.zone_high:
                score += 0.25
                break  # only count once

        # 3 — FVG at price
        for fvg in self.find_fvg(direction=direction, active_only=True):
            if fvg.gap_low <= price <= fvg.gap_high:
                score += 0.20
                break

        # 4 — LTF structure alignment
        bull_bos = {StructureKind.BOS_BULL, StructureKind.CHOCH_BULL}
        bear_bos = {StructureKind.BOS_BEAR, StructureKind.CHOCH_BEAR}
        if direction == "BULLISH" and self._last_bos in bull_bos:
            score += 0.20
        elif direction == "BEARISH" and self._last_bos in bear_bos:
            score += 0.20

        # 5 — Swing proximity
        atr = self._compute_atr()
        if atr > 0:
            threshold = 2.0 * atr
            if direction == "BULLISH":
                sl = self.latest_swing_low()
                if sl is not None and abs(price - sl.price) <= threshold:
                    score += 0.10
            else:
                sh = self.latest_swing_high()
                if sh is not None and abs(price - sh.price) <= threshold:
                    score += 0.10

        return min(score, 1.0)

    # ==================================================================
    # Simple accessors (backward-compatible)
    # ==================================================================

    def latest_swing_high(self) -> SwingPoint | None:
        """Most recently confirmed swing high."""
        return self._latest_swing(SwingType.HIGH)

    def latest_swing_low(self) -> SwingPoint | None:
        """Most recently confirmed swing low."""
        return self._latest_swing(SwingType.LOW)

    def last_trend(self) -> StructureKind | None:
        """The most recent BOS/CHOCH kind (indicates prevailing LTF trend)."""
        return self._last_bos

    # ==================================================================
    # Internal — adaptive swing detection
    # ==================================================================

    def _detect_swings(self) -> None:
        """Confirm a swing on the candle that is ``swing_lookback`` bars old.

        A candle is a swing high/low only when:
        (a) it is the strict extremum of the surrounding window, AND
        (b) ``adaptive_swing`` is False  OR  its prominence exceeds
            ``prominence_atr_mult × ATR``.

        Condition (b) is the adaptive filter: in noisy/choppy markets the
        ATR is large relative to swing heights so the prominence threshold
        becomes tighter, suppressing micro-swings.  In trending/quiet
        markets the ATR shrinks and even modest pivots qualify.
        """
        n = len(self._candles)
        k = self.swing_lookback
        if n < 2 * k + 1:
            return

        pivot_idx = n - k - 1
        pivot = self._candles[pivot_idx]
        window = list(self._candles)[pivot_idx - k: pivot_idx + k + 1]

        max_high = max(c.high for c in window)
        min_low = min(c.low for c in window)

        if pivot.high == max_high:
            others_h = [c.high for c in window if c.timestamp != pivot.timestamp]
            prominence = pivot.high - max(others_h) if others_h else 0.0
            if self._passes_prominence(prominence):
                self._add_swing(SwingPoint(
                    SwingType.HIGH, pivot.high, pivot.timestamp,
                    pivot_idx, prominence,
                ))

        if pivot.low == min_low:
            others_l = [c.low for c in window if c.timestamp != pivot.timestamp]
            prominence = min(others_l) - pivot.low if others_l else 0.0
            if self._passes_prominence(prominence):
                self._add_swing(SwingPoint(
                    SwingType.LOW, pivot.low, pivot.timestamp,
                    pivot_idx, prominence,
                ))

    def _passes_prominence(self, prominence: float) -> bool:
        """True when the swing meets the adaptive ATR-based prominence bar."""
        if not self.adaptive_swing:
            return True
        atr = self._compute_atr()
        if atr <= 0:
            return True  # not enough history yet — accept
        return prominence >= self.prominence_atr_mult * atr

    # ==================================================================
    # Internal — order block detection
    # ==================================================================

    def _detect_order_block(
        self,
        bos_candle: Candle,
        direction: Literal["BULLISH", "BEARISH"],
    ) -> None:
        """Find and store the OB preceding a confirmed structural break.

        The BOS candle must itself be a displacement (body ≥
        ``ob_displacement_mult × ATR``).  The OB is the *last* opposing
        candle in the 10 bars immediately before the BOS candle.

        * **Bullish OB**: last *bearish* candle before a bullish BOS.
          It marks a demand zone that price may revisit and bounce from.
        * **Bearish OB**: last *bullish* candle before a bearish BOS.
          It marks a supply zone.
        """
        atr = self._compute_atr()
        if atr <= 0:
            return
        # Gate: displacement candle must be genuinely impulsive
        if bos_candle.body < self.ob_displacement_mult * atr:
            return

        bars = list(self._candles)
        # Search window: up to 10 bars before the BOS candle
        end = len(bars) - 1          # index of bos_candle itself
        start = max(0, end - 10)

        ob_candle: Candle | None = None
        for i in range(end - 1, start - 1, -1):
            c = bars[i]
            if direction == "BULLISH" and c.is_bearish:
                ob_candle = c
                break
            if direction == "BEARISH" and c.is_bullish:
                ob_candle = c
                break

        if ob_candle is None:
            return

        self._order_blocks.append(OrderBlock(
            direction=direction,
            candle=ob_candle,
            zone_high=max(ob_candle.open, ob_candle.close),
            zone_low=min(ob_candle.open, ob_candle.close),
            timestamp=ob_candle.timestamp,
            displacement_range=bos_candle.body,
        ))

    # ==================================================================
    # Internal — fair value gap detection
    # ==================================================================

    def _detect_fvg(self) -> None:
        """Detect a FVG in the three most recently ingested candles.

        Checked each tick so that every new bar is tested once.

        Bullish FVG  →  bar[0].high < bar[2].low
        Bearish FVG  →  bar[0].low  > bar[2].high

        Gaps smaller than ``min_fvg_atr_mult × ATR`` are discarded as
        noise.
        """
        if len(self._candles) < 3:
            return

        b0 = self._candles[-3]
        b1 = self._candles[-2]   # displacement / impulse candle
        b2 = self._candles[-1]

        atr = self._compute_atr()
        min_size = self.min_fvg_atr_mult * atr  # 0 when ATR unavailable → accept

        # Bullish FVG
        if b0.high < b2.low:
            gap_size = b2.low - b0.high
            if gap_size >= min_size:
                self._fvgs.append(FairValueGap(
                    direction="BULLISH",
                    gap_high=b2.low,
                    gap_low=b0.high,
                    timestamp=b1.timestamp,
                ))

        # Bearish FVG
        if b0.low > b2.high:
            gap_size = b0.low - b2.high
            if gap_size >= min_size:
                self._fvgs.append(FairValueGap(
                    direction="BEARISH",
                    gap_high=b0.low,
                    gap_low=b2.high,
                    timestamp=b1.timestamp,
                ))

    # ==================================================================
    # Internal — mitigation
    # ==================================================================

    def _mitigate_obs(self, candle: Candle) -> None:
        """Mark OBs as mitigated when price re-enters their zone.

        * Bullish OB: mitigated when ``candle.low  ≤ ob.zone_high``
          (price dips back into demand).
        * Bearish OB: mitigated when ``candle.high ≥ ob.zone_low``
          (price pops back into supply).

        OBs that have already been mitigated are left unchanged (idempotent).
        """
        updated: deque[OrderBlock] = deque(maxlen=30)
        for ob in self._order_blocks:
            if ob.mitigated:
                updated.append(ob)
                continue
            touched = (
                (ob.direction == "BULLISH" and candle.low <= ob.zone_high)
                or (ob.direction == "BEARISH" and candle.high >= ob.zone_low)
            )
            if touched:
                updated.append(OrderBlock(
                    direction=ob.direction,
                    candle=ob.candle,
                    zone_high=ob.zone_high,
                    zone_low=ob.zone_low,
                    timestamp=ob.timestamp,
                    displacement_range=ob.displacement_range,
                    mitigated=True,
                ))
            else:
                updated.append(ob)
        self._order_blocks = updated

    def _fill_fvgs(self, candle: Candle) -> None:
        """Mark FVGs as filled once price trades fully through the gap.

        * Bullish FVG filled: ``candle.low  ≤ fvg.gap_low``
        * Bearish FVG filled: ``candle.high ≥ fvg.gap_high``
        """
        updated: deque[FairValueGap] = deque(maxlen=30)
        for fvg in self._fvgs:
            if fvg.filled:
                updated.append(fvg)
                continue
            filled = (
                (fvg.direction == "BULLISH" and candle.low <= fvg.gap_low)
                or (fvg.direction == "BEARISH" and candle.high >= fvg.gap_high)
            )
            if filled:
                updated.append(FairValueGap(
                    direction=fvg.direction,
                    gap_high=fvg.gap_high,
                    gap_low=fvg.gap_low,
                    timestamp=fvg.timestamp,
                    filled=True,
                ))
            else:
                updated.append(fvg)
        self._fvgs = updated

    # ==================================================================
    # Internal — helpers
    # ==================================================================

    def _add_swing(self, swing: SwingPoint) -> None:
        """Append a swing, deduplicating same-timestamp same-type entries."""
        if (
            self._swings
            and self._swings[-1].timestamp == swing.timestamp
            and self._swings[-1].type == swing.type
        ):
            return
        self._swings.append(swing)

    def _latest_swing(self, kind: SwingType) -> SwingPoint | None:
        for s in reversed(self._swings):
            if s.type == kind:
                return s
        return None

    def _is_sweep_high(self, candle: Candle, swing_high: float) -> bool:
        if candle.high <= swing_high or candle.close >= swing_high:
            return False
        if candle.range == 0:
            return False
        wick = candle.high - max(candle.close, candle.open)
        return wick / candle.range >= self.sweep_wick_ratio

    def _is_sweep_low(self, candle: Candle, swing_low: float) -> bool:
        if candle.low >= swing_low or candle.close <= swing_low:
            return False
        if candle.range == 0:
            return False
        wick = min(candle.close, candle.open) - candle.low
        return wick / candle.range >= self.sweep_wick_ratio

    def _compute_atr(self, period: int = 14) -> float:
        """Simple (non-smoothed) ATR over the last ``period`` bars."""
        bars = list(self._candles)
        if len(bars) < 2:
            return 0.0
        sample = bars[-(period + 1):]
        trs: list[float] = []
        for prev, cur in zip(sample, sample[1:], strict=False):
            trs.append(max(
                cur.high - cur.low,
                abs(cur.high - prev.close),
                abs(cur.low - prev.close),
            ))
        return sum(trs) / len(trs) if trs else 0.0
