"""Options trade analyzer for evaluating opportunities."""

import logging
from dataclasses import dataclass, field
from typing import Optional

from .etrade_client import OptionContract
from .utils import calculate_annualized_yield, format_currency, format_percentage

logger = logging.getLogger("options-bot")


@dataclass
class TradeOpportunity:
    """Represents an analyzed options trade opportunity."""
    contract: OptionContract
    underlying_price: float
    premium: float  # Using bid price for conservative estimate
    annualized_yield: float
    break_even_price: float
    downside_protection: float  # Percentage below current price
    
    # Quote metadata for email display
    previous_close: Optional[float] = None
    price_change: Optional[float] = None
    price_change_pct: Optional[float] = None
    quote_timestamp: Optional[str] = None
    next_earning_date: Optional[str] = None
    
    # Scoring and filtering metadata
    score: float = 0.0
    passes_filter: bool = True
    filter_failures: list[str] = field(default_factory=list)
    
    @property
    def return_if_assigned(self) -> float:
        """Calculate total return if option is assigned at expiration."""
        # For puts: you keep premium but buy at strike
        # Effective cost basis = strike - premium
        cost_basis = self.contract.strike_price - self.premium
        return (self.underlying_price - cost_basis) / cost_basis
    
    def to_dict(self) -> dict:
        """Convert to dictionary for reporting."""
        return {
            "underlying": self.contract.underlying,
            "option_symbol": self.contract.symbol,
            "option_type": self.contract.option_type,
            "strike": self.contract.strike_price,
            "expiration": self.contract.expiration_date.isoformat(),
            "days_to_expiry": self.contract.days_to_expiry,
            "bid": self.contract.bid,
            "ask": self.contract.ask,
            "underlying_price": self.underlying_price,
            "premium": self.premium,
            "annualized_yield": self.annualized_yield,
            "break_even": self.break_even_price,
            "downside_protection": self.downside_protection,
            "delta": self.contract.delta,
            "open_interest": self.contract.open_interest,
            "volume": self.contract.volume,
            "implied_volatility": self.contract.implied_volatility,
            "score": self.score,
        }
    
    def summary(self) -> str:
        """Generate a human-readable summary."""
        return (
            f"{self.contract.underlying} {self.contract.strike_price} "
            f"{self.contract.option_type} @ {self.contract.expiration_date} | "
            f"Bid: {format_currency(self.contract.bid)} | "
            f"Yield: {format_percentage(self.annualized_yield)} | "
            f"Protection: {format_percentage(self.downside_protection)}"
        )


class OptionsAnalyzer:
    """Analyzes options contracts and filters based on criteria."""
    
    def __init__(self, thresholds: dict):
        """Initialize the analyzer with filtering thresholds.
        
        Args:
            thresholds: Dictionary of threshold parameters including:
                - min_annualized_yield
                - min_delta / max_delta
                - min_open_interest
                - min_volume
                - max_spread_pct
        """
        self.thresholds = thresholds
    
    def analyze_contract(
        self,
        contract: OptionContract,
        underlying_price: float
    ) -> TradeOpportunity:
        """Analyze a single options contract.
        
        Args:
            contract: The option contract to analyze
            underlying_price: Current price of the underlying stock
            
        Returns:
            TradeOpportunity with analysis results
        """
        # Use bid price as the premium (worst-case execution)
        premium = contract.bid
        
        # Calculate annualized yield
        annualized = calculate_annualized_yield(
            premium,
            contract.strike_price,
            contract.days_to_expiry
        )
        
        # Calculate break-even price (for puts: strike - premium)
        if contract.option_type.upper() == "PUT":
            break_even = contract.strike_price - premium
        else:  # CALL
            break_even = contract.strike_price + premium
        
        # Calculate downside protection (for puts)
        if contract.option_type.upper() == "PUT":
            downside_protection = (underlying_price - break_even) / underlying_price
        else:
            downside_protection = 0.0  # Not applicable for calls in this context
        
        opportunity = TradeOpportunity(
            contract=contract,
            underlying_price=underlying_price,
            premium=premium,
            annualized_yield=annualized,
            break_even_price=break_even,
            downside_protection=downside_protection,
        )
        
        # Apply filters
        self._apply_filters(opportunity)
        
        # Calculate score
        opportunity.score = self._calculate_score(opportunity)
        
        return opportunity
    
    def _apply_filters(self, opportunity: TradeOpportunity) -> None:
        """Apply threshold filters to an opportunity."""
        failures = []
        
        # Yield filter
        min_yield = self.thresholds.get('min_annualized_yield', 0)
        if opportunity.annualized_yield < min_yield:
            failures.append(f"yield {format_percentage(opportunity.annualized_yield)} < {format_percentage(min_yield)}")
        
        # Delta filter (use absolute value for puts)
        delta = opportunity.contract.delta
        if delta is not None:
            abs_delta = abs(delta)
            min_delta = self.thresholds.get('min_delta', 0)
            max_delta = self.thresholds.get('max_delta', 1)
            if abs_delta < min_delta:
                failures.append(f"delta {abs_delta:.2f} < {min_delta}")
            if abs_delta > max_delta:
                failures.append(f"delta {abs_delta:.2f} > {max_delta}")
        
        # Open interest filter
        min_oi = self.thresholds.get('min_open_interest', 0)
        if opportunity.contract.open_interest < min_oi:
            failures.append(f"OI {opportunity.contract.open_interest} < {min_oi}")
        
        # Volume filter
        min_vol = self.thresholds.get('min_volume', 0)
        if opportunity.contract.volume < min_vol:
            failures.append(f"volume {opportunity.contract.volume} < {min_vol}")
        
        # Spread filter
        max_spread = self.thresholds.get('max_spread_pct', 1.0)
        if opportunity.contract.spread_pct > max_spread:
            failures.append(f"spread {format_percentage(opportunity.contract.spread_pct)} > {format_percentage(max_spread)}")
        
        # Bid must be positive
        if opportunity.contract.bid <= 0:
            failures.append("no bid price")
        
        opportunity.filter_failures = failures
        opportunity.passes_filter = len(failures) == 0
    
    def _calculate_score(self, opportunity: TradeOpportunity) -> float:
        """Calculate a composite score for ranking opportunities.
        
        Higher score = better opportunity.
        
        Conservative scoring strategy:
        - Yield: Good returns matter, but not at any cost (0-30 pts)
        - Protection: Downside buffer is important (0-25 pts)
        - Delta Safety: Lower delta = safer = better (0-25 pts)
        - Liquidity: Need to be able to exit (0-15 pts)
        - Spread: Execution cost matters (0-5 pts)
        
        This favors the lowest delta option that still offers good yield.
        """
        score = 0.0
        
        # Yield component (0-30 points)
        # 15% yield = 15 pts, 30% = 30 pts (capped)
        yield_score = min(opportunity.annualized_yield * 100, 30)
        score += yield_score
        
        # Protection component (0-25 points)
        # 10% protection = 25 pts
        protection_score = min(opportunity.downside_protection * 250, 25)
        score += protection_score
        
        # Delta Safety component (0-25 points)
        # Lower delta = higher score (safer options rank better)
        # Delta 0.15 = 25 pts, Delta 0.35 = 0 pts
        delta = opportunity.contract.delta
        if delta is not None:
            abs_delta = abs(delta)
            # Linear scale: delta 0.15 -> 25pts, delta 0.35 -> 0pts
            delta_score = max(0, 25 - ((abs_delta - 0.15) / 0.20) * 25)
            score += delta_score
        
        # Liquidity component (0-15 points)
        oi_score = min(opportunity.contract.open_interest / 1000, 7.5)  # Max at 7500 OI
        vol_score = min(opportunity.contract.volume / 100, 7.5)  # Max at 750 volume
        score += oi_score + vol_score
        
        # Spread tightness (0-5 points)
        spread_pct = opportunity.contract.spread_pct
        if spread_pct > 0:
            spread_score = max(0, 5 - (spread_pct * 25))
        else:
            spread_score = 5
        score += spread_score
        
        return round(score, 2)
    
    def analyze_chain(
        self,
        contracts: list[OptionContract],
        underlying_price: float,
        filter_passing_only: bool = True
    ) -> list[TradeOpportunity]:
        """Analyze a list of option contracts.
        
        Args:
            contracts: List of option contracts to analyze
            underlying_price: Current price of the underlying stock
            filter_passing_only: If True, only return opportunities that pass all filters
            
        Returns:
            List of TradeOpportunity objects, sorted by score (descending)
        """
        opportunities = []
        
        for contract in contracts:
            opp = self.analyze_contract(contract, underlying_price)
            
            if filter_passing_only and not opp.passes_filter:
                logger.debug(f"Filtered out {contract.symbol}: {opp.filter_failures}")
                continue
            
            opportunities.append(opp)
        
        # Sort by score (highest first)
        opportunities.sort(key=lambda x: x.score, reverse=True)
        
        logger.info(f"Found {len(opportunities)} opportunities from {len(contracts)} contracts")
        return opportunities
    
    def get_top_opportunities(
        self,
        opportunities: list[TradeOpportunity],
        top_n: int = 5
    ) -> list[TradeOpportunity]:
        """Get the top N opportunities by score.
        
        Args:
            opportunities: List of analyzed opportunities
            top_n: Number of top opportunities to return
            
        Returns:
            Top N opportunities
        """
        return opportunities[:top_n]
