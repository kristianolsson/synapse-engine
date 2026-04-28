"""E*TRADE API client for fetching market and options data."""

import logging
from datetime import date
from typing import Optional
from dataclasses import dataclass

import pyetrade

logger = logging.getLogger("options-bot")


@dataclass
class OptionContract:
    """Represents a single option contract."""
    symbol: str
    underlying: str
    option_type: str  # "CALL" or "PUT"
    strike_price: float
    expiration_date: date
    bid: float
    ask: float
    last_price: float
    volume: int
    open_interest: int
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    implied_volatility: Optional[float] = None
    
    @property
    def mid_price(self) -> float:
        """Calculate mid price between bid and ask."""
        return (self.bid + self.ask) / 2
    
    @property
    def spread(self) -> float:
        """Calculate bid-ask spread."""
        return self.ask - self.bid
    
    @property
    def spread_pct(self) -> float:
        """Calculate bid-ask spread as percentage of mid price."""
        if self.mid_price > 0:
            return self.spread / self.mid_price
        return float('inf')
    
    @property
    def days_to_expiry(self) -> int:
        """Calculate days until expiration."""
        return (self.expiration_date - date.today()).days


@dataclass
class Position:
    """Represents an option position in the account."""
    symbol: str  # Option symbol (e.g., "AAPL--240215P00175000")
    underlying: str  # Underlying ticker (e.g., "AAPL")
    option_type: str  # "PUT" or "CALL"
    strike_price: float
    expiration_date: date
    quantity: int  # Negative for short positions
    current_price: float  # Current option price (per share)
    cost_basis_per_share: float  # Original premium per share (positive for credits)
    
    @property
    def days_to_expiry(self) -> int:
        """Calculate days until expiration."""
        return (self.expiration_date - date.today()).days
    
    @property
    def current_value(self) -> float:
        """Current position value (negative for short positions)."""
        return self.current_price * 100 * self.quantity
    
    @property
    def original_credit(self) -> float:
        """Original premium received (positive for short positions)."""
        return self.cost_basis_per_share * 100 * abs(self.quantity)
    
    @property
    def profit_loss(self) -> float:
        """Current P/L in dollars. Positive = profit for short positions."""
        # For short positions: profit when current_value is less negative than cost
        # Original credit received - current cost to close
        return self.original_credit - (self.current_price * 100 * abs(self.quantity))
    
    @property
    def profit_loss_pct(self) -> float:
        """Current P/L as percentage of original credit."""
        if self.original_credit > 0:
            return self.profit_loss / self.original_credit
        return 0.0


class ETradeClient:
    """Client for interacting with E*TRADE API."""
    
    def __init__(
        self,
        consumer_key: str,
        consumer_secret: str,
        access_token: str,
        access_token_secret: str,
        sandbox: bool = True,
        account_suffix: Optional[str] = None
    ):
        """Initialize the E*TRADE client.
        
        Args:
            consumer_key: E*TRADE API consumer key
            consumer_secret: E*TRADE API consumer secret
            access_token: OAuth access token
            access_token_secret: OAuth access token secret
            sandbox: If True, use sandbox environment
            account_suffix: Optional account number suffix to filter by (e.g., "2057")
        """
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.access_token = access_token
        self.access_token_secret = access_token_secret
        self.sandbox = sandbox
        self.account_suffix = account_suffix
        
        self._market = None
        self._accounts = None
    
    @property
    def market(self) -> pyetrade.ETradeMarket:
        """Get or create the market data client."""
        if self._market is None:
            self._market = pyetrade.ETradeMarket(
                self.consumer_key,
                self.consumer_secret,
                self.access_token,
                self.access_token_secret,
                dev=self.sandbox
            )
        return self._market
    
    @property
    def accounts(self) -> pyetrade.ETradeAccounts:
        """Get or create the accounts client."""
        if self._accounts is None:
            self._accounts = pyetrade.ETradeAccounts(
                self.consumer_key,
                self.consumer_secret,
                self.access_token,
                self.access_token_secret,
                dev=self.sandbox
            )
        return self._accounts
    
    def get_buying_power(self) -> Optional[dict]:
        """Get account buying power information.
        
        Returns:
            Dictionary with buying power info, or None if unavailable.
            Contains: margin_buying_power, cash_buying_power, account_value
        """
        def safe_float(val, default=0.0):
            """Convert value to float, handling strings and None."""
            if val is None:
                return default
            try:
                return float(val)
            except (ValueError, TypeError):
                return default
        
        try:
            # First list accounts to get account ID
            accounts_data = self.accounts.list_accounts()
            
            if not accounts_data:
                logger.warning("No accounts found")
                return None
            
            # Get the first brokerage account
            account_list = accounts_data.get('AccountListResponse', {}).get('Accounts', {}).get('Account', [])
            if not account_list:
                logger.warning("No accounts in account list")
                return None
            
            # Handle single account vs list
            if isinstance(account_list, dict):
                account_list = [account_list]
            
            # Find account by suffix or take first
            account_key = None
            for acct in account_list:
                acct_id = acct.get('accountId', '')
                acct_key = acct.get('accountIdKey')
                
                # If suffix specified, match it
                if self.account_suffix:
                    if str(acct_id).endswith(self.account_suffix):
                        account_key = acct_key
                        logger.info(f"Found account ending in {self.account_suffix}")
                        break
                else:
                    # No suffix specified, take first account
                    account_key = acct_key
                    break
            
            if not account_key:
                logger.warning("Could not find account ID key")
                return None
            
            # Get account balance
            balance_data = self.accounts.get_account_balance(
                account_id_key=account_key
            )
            
            if not balance_data:
                logger.warning("No balance data returned")
                return None
            
            # Extract buying power from response - E*TRADE returns strings
            balance_response = balance_data.get('BalanceResponse', {})
            computed = balance_response.get('Computed', {})
            
            # Get values with safe conversion
            margin_bp = safe_float(computed.get('marginBuyingPower'))
            cash_bp = safe_float(computed.get('cashBuyingPower'))
            
            # Try multiple paths for account value
            account_val = safe_float(
                computed.get('RealTimeValues', {}).get('totalAccountValue')
            ) or safe_float(
                balance_response.get('accountBalance')
            ) or safe_float(
                computed.get('accountBalance')
            )
            
            cash_avail = safe_float(computed.get('cashAvailableForInvestment'))
            
            result = {
                'margin_buying_power': margin_bp,
                'cash_buying_power': cash_bp,
                'account_value': account_val,
                'cash_available': cash_avail,
            }
            
            logger.info(f"Retrieved account buying power: ${margin_bp:,.2f}")
            return result
            
        except Exception as e:
            logger.warning(f"Failed to get buying power: {e}")
            return None
    
    def get_short_put_positions(self) -> list[Position]:
        """Get all short put positions from the account.
        
        Returns:
            List of Position objects for short put positions.
        """
        def safe_float(val, default=0.0):
            """Convert value to float, handling strings and None."""
            if val is None:
                return default
            try:
                return float(val)
            except (ValueError, TypeError):
                return default
        
        positions = []
        
        try:
            # First list accounts to get account ID
            accounts_data = self.accounts.list_accounts()
            
            if not accounts_data:
                logger.warning("No accounts found")
                return positions
            
            account_list = accounts_data.get('AccountListResponse', {}).get('Accounts', {}).get('Account', [])
            if not account_list:
                logger.warning("No accounts in account list")
                return positions
            
            if isinstance(account_list, dict):
                account_list = [account_list]
            
            # Find account by suffix or take first
            account_key = None
            for acct in account_list:
                acct_id = acct.get('accountId', '')
                acct_key = acct.get('accountIdKey')
                
                if self.account_suffix:
                    if str(acct_id).endswith(self.account_suffix):
                        account_key = acct_key
                        break
                else:
                    account_key = acct_key
                    break
            
            if not account_key:
                logger.warning("Could not find account ID key")
                return positions
            
            # Get portfolio with lot data for cost basis
            portfolio_data = self.accounts.get_account_portfolio(
                account_id_key=account_key,
                lots_required=True
            )
            
            if not portfolio_data:
                logger.info("No portfolio data returned")
                return positions
            
            # Navigate to positions
            portfolio_response = portfolio_data.get('PortfolioResponse', {})
            account_portfolios = portfolio_response.get('AccountPortfolio', [])
            
            if isinstance(account_portfolios, dict):
                account_portfolios = [account_portfolios]
            
            for account_portfolio in account_portfolios:
                position_list = account_portfolio.get('Position', [])
                
                if isinstance(position_list, dict):
                    position_list = [position_list]
                
                for pos in position_list:
                    # Check if this is an option position
                    product = pos.get('Product', {})
                    security_type = product.get('securityType', '')
                    
                    # Skip non-option positions
                    if security_type != 'OPTN':
                        continue
                    
                    # Get position details
                    quantity = int(pos.get('quantity', 0))
                    
                    # Skip long positions (we only want short puts)
                    if quantity >= 0:
                        continue
                    
                    # Check if it's a PUT
                    option_type = product.get('callPut', '')
                    if option_type != 'PUT':
                        continue
                    
                    # Parse the option details
                    symbol = product.get('symbol', '')
                    underlying = product.get('securitySubType', '') or symbol[:4].rstrip('-')  # Fallback extraction
                    
                    # Try to get underlying from symbol if not present
                    if not underlying and symbol:
                        # E*TRADE option symbols often start with underlying
                        underlying = ''.join(c for c in symbol.split('--')[0] if c.isalpha())
                    
                    strike_price = safe_float(product.get('strikePrice', 0))
                    
                    # Parse expiration date
                    exp_year = product.get('expiryYear')
                    exp_month = product.get('expiryMonth')
                    exp_day = product.get('expiryDay')
                    
                    if exp_year and exp_month and exp_day:
                        expiration_date = date(int(exp_year), int(exp_month), int(exp_day))
                    else:
                        logger.warning(f"Could not parse expiration for {symbol}")
                        continue
                    
                    # Log raw position data for debugging cost basis issues
                    logger.debug(f"Raw position data for {symbol}: {pos}")
                    
                    # Get current price (use market value or quick view data)
                    quick = pos.get('Quick', {})
                    current_price = safe_float(quick.get('lastTrade', 0))
                    if current_price == 0:
                        current_price = safe_float(pos.get('marketValue', 0)) / (abs(quantity) * 100)
                    
                    # Get cost basis - try multiple E*TRADE field names
                    cost_basis_per_share = 0.0
                    
                    # Method 1: Direct per-share fields
                    price_paid = safe_float(pos.get('pricePaid', 0))
                    if price_paid > 0:
                        cost_basis_per_share = price_paid
                        logger.debug(f"Cost basis from pricePaid: {price_paid}")
                    
                    # Method 2: costPerShare field
                    if cost_basis_per_share == 0:
                        cost_per_share = safe_float(pos.get('costPerShare', 0))
                        if cost_per_share > 0:
                            cost_basis_per_share = cost_per_share
                            logger.debug(f"Cost basis from costPerShare: {cost_per_share}")
                    
                    # Method 3: Total cost basis divided by quantity
                    if cost_basis_per_share == 0:
                        cost_basis_total = safe_float(pos.get('costBasis', 0))
                        if cost_basis_total != 0 and quantity != 0:
                            cost_basis_per_share = abs(cost_basis_total) / (abs(quantity) * 100)
                            logger.debug(f"Cost basis from costBasis total: {cost_basis_total} -> per share: {cost_basis_per_share}")
                    
                    # Method 4: totalCost field
                    if cost_basis_per_share == 0:
                        total_cost = safe_float(pos.get('totalCost', 0))
                        if total_cost != 0 and quantity != 0:
                            cost_basis_per_share = abs(total_cost) / (abs(quantity) * 100)
                            logger.debug(f"Cost basis from totalCost: {total_cost} -> per share: {cost_basis_per_share}")
                    
                    # Method 5: Look in PositionLot for more accurate data
                    position_lots = pos.get('PositionLot', [])
                    if isinstance(position_lots, dict):
                        position_lots = [position_lots]
                    
                    if position_lots:
                        logger.debug(f"PositionLot data for {symbol}: {position_lots}")
                        
                        # Try multiple lot-level field names for price
                        lot_price_fields = ['price', 'pricePaid', 'orderPrice', 'totalCostForPosition']
                        
                        for price_field in lot_price_fields:
                            total_lot_cost = 0
                            total_lot_qty = 0
                            found_data = False
                            
                            for lot in position_lots:
                                lot_price = safe_float(lot.get(price_field, 0))
                                lot_qty = abs(int(lot.get('remainingQty', 0) or lot.get('originalQty', 0)))
                                
                                if lot_price > 0 and lot_qty > 0:
                                    found_data = True
                                    if price_field == 'totalCostForPosition':
                                        # This is already total, not per-unit
                                        total_lot_cost += lot_price
                                    else:
                                        total_lot_cost += lot_qty * lot_price
                                    total_lot_qty += lot_qty
                            
                            if found_data and total_lot_qty > 0:
                                lot_cost_per_share = total_lot_cost / total_lot_qty
                                logger.debug(f"Cost basis from lot field '{price_field}': {lot_cost_per_share}")
                                cost_basis_per_share = lot_cost_per_share
                                break  # Use the first field that has data
                    
                    # Warn if we still couldn't find cost basis
                    if cost_basis_per_share == 0:
                        logger.warning(
                            f"Could not determine cost basis for {underlying} ${strike_price} "
                            f"- P/L will show 0%. Run with --verbose to see raw API data."
                        )
                    
                    position = Position(
                        symbol=symbol,
                        underlying=underlying,
                        option_type='PUT',
                        strike_price=strike_price,
                        expiration_date=expiration_date,
                        quantity=quantity,
                        current_price=current_price,
                        cost_basis_per_share=cost_basis_per_share
                    )
                    
                    positions.append(position)
                    logger.info(
                        f"Short put: {underlying} ${strike_price} exp {expiration_date} | "
                        f"current_price={current_price:.4f}, cost_basis={cost_basis_per_share:.4f}, "
                        f"P/L={position.profit_loss_pct:+.1%}"
                    )
            
            logger.info(f"Found {len(positions)} short put positions")
            return positions
            
        except Exception as e:
            logger.warning(f"Failed to get positions: {e}")
            return positions
    
    def get_quote(self, symbol: str) -> dict:
        """Get current quote for a symbol.
        
        Args:
            symbol: Ticker symbol
            
        Returns:
            Quote data dictionary
        """
        logger.debug(f"Fetching quote for {symbol}")
        response = self.market.get_quote([symbol], resp_format='json')
        
        if 'QuoteResponse' in response and 'QuoteData' in response['QuoteResponse']:
            quote_data = response['QuoteResponse']['QuoteData']
            if isinstance(quote_data, list) and len(quote_data) > 0:
                return quote_data[0]
        
        raise ValueError(f"No quote data found for {symbol}")
    
    def get_option_expiration_dates(self, symbol: str) -> list[date]:
        """Get available option expiration dates for a symbol.
        
        Args:
            symbol: Ticker symbol
            
        Returns:
            List of expiration dates
        """
        logger.debug(f"Fetching expiration dates for {symbol}")
        response = self.market.get_option_expire_date(symbol, resp_format='json')
        
        dates = []
        if 'OptionExpireDateResponse' in response:
            expire_data = response['OptionExpireDateResponse'].get('ExpirationDate', [])
            
            for exp in expire_data:
                year = exp.get('year')
                month = exp.get('month')
                day = exp.get('day')
                if year and month and day:
                    dates.append(date(year, month, day))
        
        logger.info(f"Found {len(dates)} expiration dates for {symbol}")
        return sorted(dates)
    
    def get_option_chain(
        self,
        symbol: str,
        expiration_date: date,
        option_type: str = "PUT",
        strike_price_near: Optional[float] = None,
        no_of_strikes: int = 20
    ) -> list[OptionContract]:
        """Get option chain for a symbol and expiration date.
        
        Args:
            symbol: Ticker symbol
            expiration_date: Target expiration date
            option_type: "PUT" or "CALL"
            strike_price_near: Center strikes around this price (default: current stock price)
            no_of_strikes: Number of strikes to return
            
        Returns:
            List of OptionContract objects
        """
        logger.debug(f"Fetching {option_type} options for {symbol} expiring {expiration_date}")
        
        # Get current price if not specified
        if strike_price_near is None:
            quote = self.get_quote(symbol)
            all_data = quote.get('All', {})
            strike_price_near = all_data.get('lastTrade', all_data.get('ask', 0))
        
        # Convert strike_price_near to int as required by pyetrade
        strike_price_near_int = int(strike_price_near) if strike_price_near else None
        
        response = self.market.get_option_chains(
            underlier=symbol,
            expiry_date=expiration_date,
            chain_type=option_type.lower(),
            strike_price_near=strike_price_near_int,
            no_of_strikes=no_of_strikes,
            option_category="STANDARD",
            skip_adjusted=True,
            resp_format='json'
        )
        
        contracts = []
        
        if 'OptionChainResponse' in response:
            option_pairs = response['OptionChainResponse'].get('OptionPair', [])
            
            # The API returns 'Put' or 'Call' as keys (capitalized)
            option_key = option_type.capitalize()  # "PUT" -> "Put", "CALL" -> "Call"
            
            for pair in option_pairs:
                # Get the appropriate option (Put or Call)
                option_data = pair.get(option_key)
                
                if option_data:
                    contract = self._parse_option_data(option_data, symbol, expiration_date)
                    if contract:
                        contracts.append(contract)
        
        logger.info(f"Retrieved {len(contracts)} {option_type} contracts for {symbol}")
        return contracts
    
    def _parse_option_data(
        self,
        data: dict,
        underlying: str,
        expiration_date: date
    ) -> Optional[OptionContract]:
        """Parse raw option data into an OptionContract.
        
        Args:
            data: Raw option data from API
            underlying: Underlying ticker symbol
            expiration_date: Option expiration date
            
        Returns:
            OptionContract object or None if parsing fails
        """
        try:
            greeks = data.get('OptionGreeks', {})
            
            return OptionContract(
                symbol=data.get('symbol', ''),
                underlying=underlying,
                option_type=data.get('optionType', 'UNKNOWN'),
                strike_price=float(data.get('strikePrice', 0)),
                expiration_date=expiration_date,
                bid=float(data.get('bid', 0)),
                ask=float(data.get('ask', 0)),
                last_price=float(data.get('lastPrice', 0)),
                volume=int(data.get('volume', 0)),
                open_interest=int(data.get('openInterest', 0)),
                delta=greeks.get('delta'),
                gamma=greeks.get('gamma'),
                theta=greeks.get('theta'),
                vega=greeks.get('vega'),
                implied_volatility=greeks.get('iv'),
            )
        except (TypeError, ValueError) as e:
            logger.warning(f"Failed to parse option data: {e}")
            return None
    
    def get_options_for_ticker(
        self,
        symbol: str,
        target_days: int = 45,
        min_days: int = 30,
        max_days: int = 60,
        option_type: str = "PUT",
        no_of_strikes: int = 20
    ) -> tuple[Optional[date], list[OptionContract]]:
        """Get options chain for a ticker at the best expiration date.
        
        Convenience method that finds the best expiration date and fetches
        the option chain in one call.
        
        Args:
            symbol: Ticker symbol
            target_days: Target days to expiry
            min_days: Minimum days to expiry
            max_days: Maximum days to expiry
            option_type: "PUT" or "CALL"
            no_of_strikes: Number of strikes to fetch
            
        Returns:
            Tuple of (expiration_date, list of contracts)
        """
        from .utils import find_target_expiration
        
        # Get available expiration dates
        exp_dates = self.get_option_expiration_dates(symbol)
        
        # Find best expiration
        target_exp = find_target_expiration(exp_dates, target_days, min_days, max_days)
        
        if target_exp is None:
            logger.warning(f"No suitable expiration date found for {symbol}")
            return None, []
        
        logger.info(f"Selected expiration {target_exp} for {symbol} ({(target_exp - date.today()).days} days out)")
        
        # Get option chain
        contracts = self.get_option_chain(
            symbol,
            target_exp,
            option_type=option_type,
            no_of_strikes=no_of_strikes
        )
        
        return target_exp, contracts
