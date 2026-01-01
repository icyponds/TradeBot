"""
Delta-Neutral Funding Rate Arbitrage Strategy.

This strategy captures funding rate payments while maintaining market neutrality
by simultaneously holding opposite positions in perpetual futures and spot markets.
"""

import logging
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy


@dataclass
class FundingArbPosition:
    """Tracks a funding rate arbitrage position."""
    symbol: str
    perp_side: str  # 'long' or 'short'
    perp_size: float
    perp_entry_price: float
    spot_side: str  # opposite of perp_side
    spot_size: float
    spot_entry_price: float
    entry_funding_rate: float
    entry_time: datetime
    funding_payments_received: float = 0.0
    last_funding_time: Optional[datetime] = None
    
    @property
    def net_delta(self) -> float:
        """Calculate net delta (should be ~0 for delta neutral)."""
        perp_delta = self.perp_size if self.perp_side == 'long' else -self.perp_size
        spot_delta = self.spot_size if self.spot_side == 'long' else -self.spot_size
        return perp_delta + spot_delta
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'perp_side': self.perp_side,
            'perp_size': self.perp_size,
            'perp_entry_price': self.perp_entry_price,
            'spot_side': self.spot_side,
            'spot_size': self.spot_size,
            'spot_entry_price': self.spot_entry_price,
            'entry_funding_rate': self.entry_funding_rate,
            'entry_time': self.entry_time.isoformat(),
            'funding_payments_received': self.funding_payments_received,
            'net_delta': self.net_delta,
        }


class FundingRateArbitrageStrategy(BaseStrategy):
    """
    Delta-Neutral Funding Rate Arbitrage Strategy.
    
    This strategy exploits funding rate differentials in perpetual futures
    while maintaining market neutrality through spot hedging.
    
    Strategy Logic:
    - When funding rate is significantly POSITIVE (longs pay shorts):
        - SHORT perpetual (receive funding payments)
        - LONG spot (hedge for delta neutrality)
        
    - When funding rate is significantly NEGATIVE (shorts pay longs):
        - LONG perpetual (receive funding payments)
        - SHORT spot (hedge for delta neutrality)
    
    P&L = Funding payments received - Trading fees - Slippage
    """
    
    # Funding rates are paid every 8 hours, 1h timeframe is optimal for monitoring
    PREFERRED_TIMEFRAME = '1h'
    
    def __init__(self, config: Dict[str, Any], market_api=None):
        super().__init__(config)
        
        # Strategy parameters from config
        arb_config = config.get('strategies', {}).get('funding_rate_arbitrage', {})
        
        # Entry/exit thresholds (as decimals, e.g., 0.0003 = 0.03%)
        self.entry_threshold = arb_config.get('entry_threshold', 0.0003)  # 0.03% per 8h ~= 41% APR
        self.exit_threshold = arb_config.get('exit_threshold', 0.0001)    # 0.01% per 8h
        
        # Position management
        self.max_position_pct = arb_config.get('max_position_pct', 20)    # Max % of portfolio per arb
        self.min_holding_periods = arb_config.get('min_holding_periods', 1)  # Min funding periods to hold
        self.rebalance_threshold = arb_config.get('rebalance_threshold', 0.02)  # Rebalance if delta drifts > 2%
        
        # Funding rate history settings
        self.funding_history_periods = arb_config.get('funding_history_periods', 24)  # 24 * 8h = 8 days
        self.min_consistent_periods = arb_config.get('min_consistent_periods', 3)  # Min periods funding > threshold
        
        # Market API for executing trades
        self.market_api = market_api
        
        # Active arbitrage positions
        self.arb_positions: Dict[str, FundingArbPosition] = {}
        
        # Funding rate cache
        self.funding_rate_cache: Dict[str, List[Tuple[datetime, float]]] = {}
        
        # Hyperliquid funding interval (8 hours)
        self.funding_interval_hours = 8
        
        self.logger.info(f"Initialized Funding Rate Arbitrage Strategy: "
                        f"entry_threshold={self.entry_threshold:.4%}, "
                        f"exit_threshold={self.exit_threshold:.4%}")
    
    def set_market_api(self, market_api):
        """Set the market API for executing trades."""
        self.market_api = market_api
    
    def generate_signal(self, ohlcv: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        Generate signal for funding rate arbitrage.
        
        Note: This strategy doesn't use OHLCV data directly.
        It needs funding rate data which should be fetched separately.
        
        Args:
            ohlcv: OHLCV data (not used for this strategy)
            
        Returns:
            Signal dictionary or None
        """
        # This strategy requires symbol context
        # Use generate_signal_for_symbol instead
        return None
    
    def generate_signal_for_symbol(self, symbol: str, funding_rate: float, 
                                   funding_history: List[float] = None) -> Optional[Dict[str, Any]]:
        """
        Generate arbitrage signal for a specific symbol.
        
        Args:
            symbol: Trading symbol (e.g., 'BTC')
            funding_rate: Current funding rate (as decimal)
            funding_history: Historical funding rates (optional, for persistence check)
            
        Returns:
            Signal dictionary or None if no opportunity
        """
        # Check if we already have a position in this symbol
        if symbol in self.arb_positions:
            return self._check_exit_signal(symbol, funding_rate)
        
        # Check entry conditions
        return self._check_entry_signal(symbol, funding_rate, funding_history)
    
    def _check_entry_signal(self, symbol: str, funding_rate: float, 
                           funding_history: List[float] = None) -> Optional[Dict[str, Any]]:
        """Check if we should enter a new arbitrage position."""
        
        abs_funding = abs(funding_rate)
        
        # Check if funding rate exceeds entry threshold
        if abs_funding < self.entry_threshold:
            return None
        
        # Check funding rate persistence if history is available
        if funding_history and len(funding_history) >= self.min_consistent_periods:
            consistent_count = sum(
                1 for rate in funding_history[-self.min_consistent_periods:]
                if abs(rate) >= self.entry_threshold * 0.8  # 80% of threshold for consistency
            )
            
            if consistent_count < self.min_consistent_periods:
                self.logger.debug(f"{symbol}: Funding rate not consistent enough "
                                f"({consistent_count}/{self.min_consistent_periods})")
                return None
        
        # Determine position direction
        if funding_rate > 0:
            # Positive funding: longs pay shorts
            # Strategy: SHORT perp + LONG spot
            perp_side = 'short'
            spot_side = 'long'
            reason = f"Positive funding {funding_rate:.4%} - shorting perp, longing spot"
        else:
            # Negative funding: shorts pay longs
            # Strategy: LONG perp + SHORT spot
            perp_side = 'long'
            spot_side = 'short'
            reason = f"Negative funding {funding_rate:.4%} - longing perp, shorting spot"
        
        # Calculate annualized return
        periods_per_year = 365 * 24 / self.funding_interval_hours  # ~1095 periods
        annualized_return = abs_funding * periods_per_year
        
        return {
            'signal': 'enter_arb',
            'symbol': symbol,
            'perp_side': perp_side,
            'spot_side': spot_side,
            'funding_rate': funding_rate,
            'annualized_return': annualized_return,
            'reason': reason,
            'strategy': 'funding_rate_arbitrage',
        }
    
    def _check_exit_signal(self, symbol: str, funding_rate: float) -> Optional[Dict[str, Any]]:
        """Check if we should exit an existing arbitrage position."""
        
        if symbol not in self.arb_positions:
            return None
        
        position = self.arb_positions[symbol]
        
        # Check minimum holding period
        time_held = datetime.now() - position.entry_time
        min_hold_time = timedelta(hours=self.funding_interval_hours * self.min_holding_periods)
        
        if time_held < min_hold_time:
            return None
        
        # Check if funding rate has normalized or reversed
        abs_funding = abs(funding_rate)
        
        # Exit if funding rate dropped below exit threshold
        if abs_funding < self.exit_threshold:
            return {
                'signal': 'exit_arb',
                'symbol': symbol,
                'reason': f"Funding rate normalized to {funding_rate:.4%}",
                'funding_payments': position.funding_payments_received,
                'strategy': 'funding_rate_arbitrage',
            }
        
        # Exit if funding rate reversed significantly (would cost us money)
        original_direction = 1 if position.perp_side == 'short' else -1
        current_direction = 1 if funding_rate > 0 else -1
        
        if original_direction != current_direction and abs_funding > self.exit_threshold:
            return {
                'signal': 'exit_arb',
                'symbol': symbol,
                'reason': f"Funding rate reversed to {funding_rate:.4%}",
                'funding_payments': position.funding_payments_received,
                'strategy': 'funding_rate_arbitrage',
            }
        
        return None
    
    def scan_opportunities(self, funding_rates: Dict[str, float]) -> List[Dict[str, Any]]:
        """
        Scan all symbols for funding rate arbitrage opportunities.
        
        Args:
            funding_rates: Dictionary mapping symbol to current funding rate
            
        Returns:
            List of opportunity dictionaries sorted by annualized return
        """
        opportunities = []
        
        for symbol, rate in funding_rates.items():
            # Get historical rates if available
            history = self.funding_rate_cache.get(symbol, [])
            history_rates = [r[1] for r in history[-self.funding_history_periods:]]
            
            signal = self.generate_signal_for_symbol(symbol, rate, history_rates)
            
            if signal and signal.get('signal') == 'enter_arb':
                opportunities.append(signal)
        
        # Sort by annualized return (descending)
        opportunities.sort(key=lambda x: x.get('annualized_return', 0), reverse=True)
        
        return opportunities
    
    def execute_arbitrage_entry(self, symbol: str, perp_side: str, spot_side: str,
                               size: float, funding_rate: float) -> Optional[FundingArbPosition]:
        """
        Execute entry into a funding rate arbitrage position.
        
        Args:
            symbol: Trading symbol
            perp_side: 'long' or 'short' for perpetual
            spot_side: 'long' or 'short' for spot (should be opposite of perp_side)
            size: Position size in base currency
            funding_rate: Current funding rate at entry
            
        Returns:
            FundingArbPosition if successful, None otherwise
        """
        if not self.market_api:
            self.logger.error("Market API not set - cannot execute trades")
            return None
        
        try:
            # Get current prices
            perp_price = self.market_api.get_current_price(symbol)
            spot_price = self.market_api.get_spot_price(symbol, "USDC")
            
            if not perp_price or not spot_price:
                self.logger.error(f"Could not get prices for {symbol}")
                return None
            
            # Execute perpetual leg
            perp_order_side = 'sell' if perp_side == 'short' else 'buy'
            perp_result = self.market_api.place_order(symbol, perp_order_side, size, None)
            
            if not perp_result:
                self.logger.error(f"Failed to place perp order for {symbol}")
                return None
            
            # Execute spot leg
            spot_order_side = 'buy' if spot_side == 'long' else 'sell'
            spot_result = self.market_api.place_spot_order(symbol, "USDC", spot_order_side, size)
            
            if not spot_result:
                self.logger.error(f"Failed to place spot order for {symbol} - unwinding perp")
                # Unwind the perp position
                unwind_side = 'buy' if perp_side == 'short' else 'sell'
                self.market_api.place_order(symbol, unwind_side, size, None)
                return None
            
            # Create position record
            position = FundingArbPosition(
                symbol=symbol,
                perp_side=perp_side,
                perp_size=size,
                perp_entry_price=perp_price,
                spot_side=spot_side,
                spot_size=size,
                spot_entry_price=spot_price,
                entry_funding_rate=funding_rate,
                entry_time=datetime.now(),
            )
            
            self.arb_positions[symbol] = position
            
            self.logger.info(f"Entered funding arb for {symbol}: "
                           f"PERP {perp_side} {size} @ {perp_price}, "
                           f"SPOT {spot_side} {size} @ {spot_price}, "
                           f"Funding: {funding_rate:.4%}")
            
            return position
            
        except Exception as e:
            self.logger.error(f"Error executing arbitrage entry for {symbol}: {e}")
            return None
    
    def execute_arbitrage_exit(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Exit a funding rate arbitrage position.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            Exit result dictionary or None if error
        """
        if symbol not in self.arb_positions:
            self.logger.warning(f"No arbitrage position found for {symbol}")
            return None
        
        if not self.market_api:
            self.logger.error("Market API not set - cannot execute trades")
            return None
        
        position = self.arb_positions[symbol]
        
        try:
            # Get current prices
            perp_price = self.market_api.get_current_price(symbol)
            spot_price = self.market_api.get_spot_price(symbol, "USDC")
            
            if not perp_price or not spot_price:
                self.logger.error(f"Could not get prices for {symbol}")
                return None
            
            # Close perpetual position
            perp_close_side = 'buy' if position.perp_side == 'short' else 'sell'
            perp_result = self.market_api.place_order(symbol, perp_close_side, position.perp_size, None)
            
            # Close spot position
            spot_close_side = 'sell' if position.spot_side == 'long' else 'buy'
            spot_result = self.market_api.place_spot_order(symbol, "USDC", spot_close_side, position.spot_size)
            
            # Calculate P&L
            perp_pnl = self._calculate_perp_pnl(position, perp_price)
            spot_pnl = self._calculate_spot_pnl(position, spot_price)
            total_pnl = perp_pnl + spot_pnl + position.funding_payments_received
            
            # Remove from active positions
            del self.arb_positions[symbol]
            
            result = {
                'symbol': symbol,
                'perp_pnl': perp_pnl,
                'spot_pnl': spot_pnl,
                'funding_received': position.funding_payments_received,
                'total_pnl': total_pnl,
                'holding_time_hours': (datetime.now() - position.entry_time).total_seconds() / 3600,
            }
            
            self.logger.info(f"Exited funding arb for {symbol}: "
                           f"Perp P&L: ${perp_pnl:.2f}, "
                           f"Spot P&L: ${spot_pnl:.2f}, "
                           f"Funding: ${position.funding_payments_received:.2f}, "
                           f"Total: ${total_pnl:.2f}")
            
            return result
            
        except Exception as e:
            self.logger.error(f"Error executing arbitrage exit for {symbol}: {e}")
            return None
    
    def _calculate_perp_pnl(self, position: FundingArbPosition, current_price: float) -> float:
        """Calculate P&L on perpetual position."""
        price_diff = current_price - position.perp_entry_price
        if position.perp_side == 'short':
            price_diff = -price_diff
        return price_diff * position.perp_size
    
    def _calculate_spot_pnl(self, position: FundingArbPosition, current_price: float) -> float:
        """Calculate P&L on spot position."""
        price_diff = current_price - position.spot_entry_price
        if position.spot_side == 'short':
            price_diff = -price_diff
        return price_diff * position.spot_size
    
    def update_funding_payment(self, symbol: str, payment_amount: float):
        """
        Record a funding payment for an active position.
        
        Args:
            symbol: Trading symbol
            payment_amount: Funding payment amount (positive if received)
        """
        if symbol in self.arb_positions:
            position = self.arb_positions[symbol]
            position.funding_payments_received += payment_amount
            position.last_funding_time = datetime.now()
            self.logger.info(f"Recorded funding payment for {symbol}: ${payment_amount:.4f}, "
                           f"Total: ${position.funding_payments_received:.4f}")
    
    def check_rebalance_needed(self, symbol: str, current_perp_price: float, 
                               current_spot_price: float) -> bool:
        """
        Check if position needs rebalancing due to delta drift.
        
        Args:
            symbol: Trading symbol
            current_perp_price: Current perpetual price
            current_spot_price: Current spot price
            
        Returns:
            True if rebalancing is needed
        """
        if symbol not in self.arb_positions:
            return False
        
        position = self.arb_positions[symbol]
        
        # Calculate current notional values
        perp_notional = position.perp_size * current_perp_price
        spot_notional = position.spot_size * current_spot_price
        
        # Check for drift
        avg_notional = (perp_notional + spot_notional) / 2
        drift = abs(perp_notional - spot_notional) / avg_notional
        
        if drift > self.rebalance_threshold:
            self.logger.warning(f"{symbol}: Delta drift {drift:.2%} exceeds threshold "
                              f"{self.rebalance_threshold:.2%}")
            return True
        
        return False
    
    def get_active_positions_summary(self) -> Dict[str, Any]:
        """Get summary of all active arbitrage positions."""
        positions = []
        total_funding = 0.0
        
        for symbol, position in self.arb_positions.items():
            positions.append(position.to_dict())
            total_funding += position.funding_payments_received
        
        return {
            'active_positions': len(positions),
            'positions': positions,
            'total_funding_received': total_funding,
        }
    
    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: pd.DataFrame = None,
                             signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """
        Calculate take profit for funding rate arbitrage.
        
        For this strategy, we don't use traditional take profit based on price.
        Instead, we exit based on funding rate normalization.
        
        Returns a very wide take profit as placeholder.
        """
        # Very wide TP since we exit based on funding rate, not price
        if side == 'buy':
            return entry_price * 1.5  # 50% - effectively no TP
        else:
            return entry_price * 0.5  # -50% - effectively no TP
    
    def update_funding_cache(self, symbol: str, funding_rate: float):
        """Update the funding rate cache for a symbol."""
        if symbol not in self.funding_rate_cache:
            self.funding_rate_cache[symbol] = []
        
        self.funding_rate_cache[symbol].append((datetime.now(), funding_rate))
        
        # Keep only recent history
        max_history = self.funding_history_periods * 2
        if len(self.funding_rate_cache[symbol]) > max_history:
            self.funding_rate_cache[symbol] = self.funding_rate_cache[symbol][-max_history:]

