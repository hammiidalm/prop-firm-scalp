"""Signal confluence scoring dataclass.

Captures the breakdown of every factor that contributed (or didn't) to a
strategy signal decision.  This is persisted in the trade journal and
displayed in Telegram notifications for full traceability.

The ``SignalConfluence`` object is attached to the ``Signal`` via the
``structure_tags`` field (as a serialized dict) so that downstream layers
(executor, notifier, journal) can present the detailed breakdown without
coupling to the strategy internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SignalConfluence:
    """Immutable record of a confluence evaluation.

    Attributes
    ----------
    score:
        Total confluence score (0–100).
    max_score:
        Theoretical maximum (always 100 in current implementation).
    factors_hit:
        Number of factors that scored > 0.
    factors_total:
        Total number of factors evaluated (always 5).
    sweep:
        Points awarded for liquidity sweep detection (0 or 40).
    rejection:
        Points awarded for strong rejection candle (0 or 25).
    bos_htf:
        Points awarded for BOS/CHOCH + HTF alignment (0, 10, or 20).
    order_block:
        Points awarded for order block proximity (0 or 10).
    session_spread:
        Points awarded for session + spread pass (0 or 5).
    direction:
        The evaluated direction ("LONG" or "SHORT").
    htf_bias:
        The HTF bias at evaluation time.
    tags:
        Full list of structural tags collected during evaluation.
    rejection_reason:
        If the signal was rejected, a human-readable reason string.
        Empty string if the signal was accepted.
    """

    score: int
    max_score: int = 100
    factors_hit: int = 0
    factors_total: int = 5
    sweep: int = 0
    rejection: int = 0
    bos_htf: int = 0
    order_block: int = 0
    session_spread: int = 0
    direction: str = ""
    htf_bias: str = ""
    tags: list[str] = field(default_factory=list)
    rejection_reason: str = ""

    @property
    def accepted(self) -> bool:
        """True if the signal passed the minimum confluence threshold."""
        return self.rejection_reason == ""

    @property
    def factors_summary(self) -> str:
        """Human-readable summary: '82/100 • 4/5 factors'."""
        return f"{self.score}/{self.max_score} • {self.factors_hit}/{self.factors_total} factors"

    def to_dict(self) -> dict[str, object]:
        """Serialize for JSON logging / journal storage."""
        return {
            "score": self.score,
            "max_score": self.max_score,
            "factors_hit": self.factors_hit,
            "factors_total": self.factors_total,
            "sweep": self.sweep,
            "rejection": self.rejection,
            "bos_htf": self.bos_htf,
            "order_block": self.order_block,
            "session_spread": self.session_spread,
            "direction": self.direction,
            "htf_bias": self.htf_bias,
            "tags": self.tags,
            "rejection_reason": self.rejection_reason,
            "accepted": self.accepted,
        }

    @classmethod
    def rejected(
        cls,
        *,
        direction: str,
        htf_bias: str,
        reason: str,
        score: int = 0,
        tags: list[str] | None = None,
        sweep: int = 0,
        rejection_pts: int = 0,
        bos_htf: int = 0,
        order_block: int = 0,
        session_spread: int = 0,
    ) -> SignalConfluence:
        """Factory for a rejected confluence evaluation."""
        factors_hit = sum(1 for v in [sweep, rejection_pts, bos_htf, order_block, session_spread] if v > 0)
        return cls(
            score=score,
            factors_hit=factors_hit,
            factors_total=5,
            sweep=sweep,
            rejection=rejection_pts,
            bos_htf=bos_htf,
            order_block=order_block,
            session_spread=session_spread,
            direction=direction,
            htf_bias=htf_bias,
            tags=tags or [],
            rejection_reason=reason,
        )
