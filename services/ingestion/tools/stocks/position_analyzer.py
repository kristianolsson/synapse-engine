"""Position analyzer for CSP exit decision recommendations."""

import logging
from dataclasses import dataclass

from .etrade_client import Position

logger = logging.getLogger("options-bot")


@dataclass
class PositionRecommendation:
    """Recommendation for an open position."""
    position: Position
    action: str  # "CLOSE", "HOLD", or "ROLL"
    reason: str  # Human-readable explanation
    urgency: str  # "high", "medium", "low"
    
    @property
    def profit_loss_pct(self) -> float:
        """Current P/L as percentage."""
        return self.position.profit_loss_pct
    
    @property
    def action_emoji(self) -> str:
        """Get emoji for the action."""
        return {
            "CLOSE": "🔴",
            "HOLD": "🟢",
            "ROLL": "🟡"
        }.get(self.action, "⚪")
    
    def summary(self) -> str:
        """Get a one-line summary of the recommendation."""
        pnl_str = f"{self.profit_loss_pct:+.1%}"
        return (
            f"{self.action_emoji} {self.action}: {self.position.underlying} "
            f"${self.position.strike_price:.0f}P exp {self.position.expiration_date} "
            f"({self.position.days_to_expiry} DTE) | P/L: {pnl_str} | {self.reason}"
        )


class PositionAnalyzer:
    """Analyzes positions and generates exit recommendations.
    
    Decision tree:
    1. Profit >= 50%? -> CLOSE (take profit)
    2. DTE <= 21 and Profit >= 25%? -> CLOSE (take profit at low DTE)
    3. DTE <= 21 and Profit < 25%? -> ROLL (consider rolling)
    4. Loss >= 2x premium? -> CLOSE (cut loss)
    5. Otherwise -> HOLD
    """
    
    def __init__(
        self,
        profit_target_pct: float = 0.50,
        max_loss_multiple: float = 2.0,
        low_dte_threshold: int = 21,
        low_dte_profit_target: float = 0.25
    ):
        """Initialize the analyzer with exit thresholds.
        
        Args:
            profit_target_pct: Close at this profit percentage (0.50 = 50%)
            max_loss_multiple: Close at this loss multiple of premium (2.0 = 2x)
            low_dte_threshold: DTE threshold for time-based decisions
            low_dte_profit_target: Profit target when DTE is low (0.25 = 25%)
        """
        self.profit_target_pct = profit_target_pct
        self.max_loss_multiple = max_loss_multiple
        self.low_dte_threshold = low_dte_threshold
        self.low_dte_profit_target = low_dte_profit_target
    
    def analyze_position(self, position: Position) -> PositionRecommendation:
        """Analyze a position and generate a recommendation.
        
        Args:
            position: The position to analyze
            
        Returns:
            PositionRecommendation with action and reason
        """
        pnl_pct = position.profit_loss_pct
        dte = position.days_to_expiry
        
        # Decision 1: High profit target reached
        if pnl_pct >= self.profit_target_pct:
            return PositionRecommendation(
                position=position,
                action="CLOSE",
                reason=f"Profit target reached ({pnl_pct:.0%} >= {self.profit_target_pct:.0%})",
                urgency="high"
            )
        
        # Decision 2: Low DTE checks
        if dte <= self.low_dte_threshold:
            if pnl_pct >= self.low_dte_profit_target:
                # Profitable at low DTE - close it
                return PositionRecommendation(
                    position=position,
                    action="CLOSE",
                    reason=f"Low DTE ({dte}d) with profit ({pnl_pct:.0%}), take gains",
                    urgency="high"
                )
            else:
                # Not profitable at low DTE - consider rolling
                return PositionRecommendation(
                    position=position,
                    action="ROLL",
                    reason=f"Low DTE ({dte}d), consider rolling out for more time/credit",
                    urgency="medium"
                )
        
        # Decision 3: Max loss threshold
        # Loss is when pnl_pct is negative; -2.0 means 200% loss (2x premium)
        if pnl_pct <= -self.max_loss_multiple:
            return PositionRecommendation(
                position=position,
                action="CLOSE",
                reason=f"Max loss reached ({pnl_pct:.0%} loss >= {self.max_loss_multiple:.0f}x premium)",
                urgency="high"
            )
        
        # Decision 4: Hold - no action needed
        if pnl_pct >= 0:
            return PositionRecommendation(
                position=position,
                action="HOLD",
                reason=f"Profitable ({pnl_pct:.0%}), {dte}d remaining",
                urgency="low"
            )
        else:
            return PositionRecommendation(
                position=position,
                action="HOLD",
                reason=f"Loss ({pnl_pct:.0%}) within limits, {dte}d remaining",
                urgency="low"
            )
    
    def analyze_positions(self, positions: list[Position]) -> list[PositionRecommendation]:
        """Analyze multiple positions.
        
        Args:
            positions: List of positions to analyze
            
        Returns:
            List of recommendations, sorted by urgency
        """
        recommendations = [self.analyze_position(pos) for pos in positions]
        
        # Sort by urgency (high first) then by action (CLOSE first)
        urgency_order = {"high": 0, "medium": 1, "low": 2}
        action_order = {"CLOSE": 0, "ROLL": 1, "HOLD": 2}
        
        recommendations.sort(
            key=lambda r: (urgency_order.get(r.urgency, 99), action_order.get(r.action, 99))
        )
        
        return recommendations
