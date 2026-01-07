"""
Delta-Neutral Funding Rate Arbitrage Strategy.

This strategy captures funding rate payments while maintaining market neutrality
by simultaneously holding opposite positions in perpetual futures and spot markets.

Signals are generated here; execution is handled by StrategyManager.
"""

import logging
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy


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
    
    This strategy generates multi-leg signals that are executed atomically
    by the StrategyManager.
    """
    
    # Funding rates are paid every 8 hours, 1h timeframe is optimal for monitoring
    PREFERRED_TIMEFRAME = '1h'
    
    def __init__(self, config: Dict[str, Any], market_api=None, timeframe: str = None):
        super().__init__(config, timeframe)
        
        # Strategy parameters from config
        arb_config = config.get('strategies', {}).get('funding_rate_arbitrage', {})
        
        # Entry/exit thresholds (as decimals, e.g., 0.0003 = 0.03%)
        self.entry_threshold = arb_config.get('entry_threshold', 0.000017)  # 0.0017% per hour ~= 15% APR
        self.exit_threshold = arb_config.get('exit_threshold', 0.000006)   # 0.0006% per hour ~= 5% APR
        
        # Position management
        self.max_position_pct = arb_config.get('max_position_pct', 20)    # Max % of portfolio per arb
        self.min_holding_periods = arb_config.get('min_holding_periods', 4)  # Min 4 hours to cover trading fees
        self.rebalance_threshold = arb_config.get('rebalance_threshold', 0.02)  # Rebalance if delta drifts > 2%
        
        # Funding rate history settings
        self.funding_history_periods = arb_config.get('funding_history_periods', 24)  # 24 * 8h = 8 days
        self.min_consistent_periods = arb_config.get('min_consistent_periods', 3)  # Min periods funding > threshold
        
        # Market API for data access (not for execution)
        self.market_api = market_api
        
        # Funding rate cache
        self.funding_rate_cache: Dict[str, List[Tuple[datetime, float]]] = {}
        
        # Cache for eligible symbols (those with both perp and spot markets)
        self._eligible_symbols_cache: Optional[set] = None
        self._eligible_symbols_last_update: Optional[datetime] = None
        self._eligible_symbols_ttl = timedelta(hours=1)  # Refresh every hour
        
        # Hyperliquid funding interval (1 hour - paid at top of each hour)
        self.funding_interval_hours = 1
        
        self.logger.info(f"Initialized Funding Rate Arbitrage Strategy: "
                        f"entry_threshold={self.entry_threshold:.4%}, "
                        f"exit_threshold={self.exit_threshold:.4%}")
    
    def set_market_api(self, market_api):
        """Set the market API for data access."""
        self.market_api = market_api
        # Invalidate cache when API changes
        self._eligible_symbols_cache = None
    
    def get_eligible_symbols(self) -> set:
        """
        Get symbols eligible for funding rate arbitrage.
        
        Returns cached result if available and fresh.
        Only symbols with BOTH perp AND spot markets can be used.
        """
        now = datetime.now()
        
        # Return cached result if fresh
        if (self._eligible_symbols_cache is not None and 
            self._eligible_symbols_last_update is not None and
            now - self._eligible_symbols_last_update < self._eligible_symbols_ttl):
            return self._eligible_symbols_cache
        
        # Fetch fresh list
        if self.market_api and hasattr(self.market_api, 'get_funding_arb_eligible_symbols'):
            eligible_list = self.market_api.get_funding_arb_eligible_symbols()
            # Handle list of dicts (from Mock) or strings
            processed_list = []
            for item in eligible_list:
                if isinstance(item, dict):
                    processed_list.append(item.get('symbol'))
                else:
                    processed_list.append(item)
            self._eligible_symbols_cache = set(processed_list)
            self._eligible_symbols_last_update = now
            self.logger.info(f"Refreshed eligible symbols: {sorted(self._eligible_symbols_cache)}")
        else:
            self._eligible_symbols_cache = set()
            self.logger.warning("Cannot get eligible symbols - market_api not available")
        
        return self._eligible_symbols_cache
    
    def is_symbol_eligible(self, symbol: str) -> bool:
        """Check if a symbol can be used for funding rate arbitrage."""
        return symbol in self.get_eligible_symbols()
    
    def generate_signal(self, symbol: str, ohlcv: Dict[str, pd.DataFrame]) -> Optional[Dict[str, Any]]:
        """
        Generate signal for funding rate arbitrage.
        
        Args:
            symbol: Symbol being analyzed
            ohlcv: OHLCV data (not used directly, but context provided)
            
        Returns:
            Signal dictionary or None
        """
        if not self.market_api:
            self.logger.warning("Market API not set for Funding Rate Arb")
            return None

        # Fetch current funding rate
        current_rate = self.market_api.get_funding_rate(symbol)
        
        if current_rate is None:
            return None
            
        # Update cache
        self.update_funding_cache(symbol, current_rate)
        
        # Get history for persistence check
        history = self.get_funding_history(symbol)
        
        # Check for existing position context (this needs to be passed or tracked)
        # For now, we rely on StrategyManager or the Strategy itself to know active positions?
        # StrategyManager handles active positions but currently doesn't pass them here.
        # However, generate_signal_for_symbol asks for `has_existing_position`.
        # We can implement a basic internal tracking or rely on a query to execution engine?
        
        # NOTE: StrategyManager tracks active positions. Ideally, it should pass this context.
        # But for now, let's assume no existing position unless we track it internally here 
        # (which base strategy doesn't strictly enforce across restarts without persistence).
        # We have `self.trades` but that's for history.
        
        # We will assume entry checking. Exit checking is typically handled by `should_exit` 
        # or explicit calls.
        
        return self.generate_signal_for_symbol(symbol, current_rate, history)
    
    def generate_signal_for_symbol(
        self, 
        symbol: str, 
        funding_rate: float,
        funding_history: List[float] = None,
        has_existing_position: bool = False,
        position_entry_time: datetime = None,
        position_perp_side: str = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Generate arbitrage signal for a specific symbol.
        
        Args:
            symbol: Trading symbol (e.g., 'BTC')
            funding_rate: Current funding rate (as decimal)
            funding_history: Historical funding rates (optional, for persistence check)
            has_existing_position: Whether we have an existing multi-leg position
            position_entry_time: Entry time of existing position (for min hold check)
            position_perp_side: Perp side of existing position ('long' or 'short')
            
        Returns:
            Signal dictionary with multi-leg information or None if no opportunity
        """
        # Check exit conditions first if we have a position
        if has_existing_position:
            return self._check_exit_signal(
                symbol, funding_rate, position_entry_time, position_perp_side
            )
        
        # Check entry conditions
        return self._check_entry_signal(symbol, funding_rate, funding_history)
    
    def _check_entry_signal(
        self, 
        symbol: str, 
        funding_rate: float,
        funding_history: List[float] = None
    ) -> Optional[Dict[str, Any]]:
        """Check if we should enter a new arbitrage position."""
        
        # CRITICAL: Check if symbol has a spot market for delta-neutral hedging
        # Only a few tokens on Hyperliquid have both perp and spot markets
        # (e.g., BERA, HYPE, MON, PUMP, PURR, STABLE, TRUMP as of early 2026)
        if not self.is_symbol_eligible(symbol):
            self.logger.debug(f"{symbol}: No spot market available - cannot create delta-neutral position")
            return None
        
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
        
        # Yield-Weighted Sizing (Phase 7 Refinement)
        # Scale position size based on attractiveness of yield
        # Base case: 10% APR = 1.0x sizing multiplier
        # Max case: 50% APR = 2.0x sizing multiplier
        base_yield = 0.10  # 10%
        size_multiplier = min(2.0, max(0.5, annualized_return / base_yield))
        
        reason += f" (APR: {annualized_return:.1%}, Size: {size_multiplier:.1f}x)"
        
        # Return multi-leg signal
        return {
            'signal': 'buy',  # Standard signal type for entry
            'signal_type': 'multi_leg',
            'action': 'enter',
            'symbol': symbol,
            'reason': reason,
            'strategy': 'funding_rate_arbitrage',
            'funding_rate': funding_rate,
            'annualized_return': annualized_return,
            'size_multiplier': size_multiplier,
            # Multi-leg specification
            'legs': [
                {
                    'symbol': symbol,
                    'market_type': 'perp',
                    'side': perp_side,
                    'order_side': 'sell' if perp_side == 'short' else 'buy',
                },
                {
                    'symbol': f"{symbol}/USDC",
                    'market_type': 'spot',
                    'side': spot_side,
                    'order_side': 'buy' if spot_side == 'long' else 'sell',
                },
            ],
            'atomic': True,  # If one leg fails, unwind the other
            'metadata': {
                'entry_funding_rate': funding_rate,
                'perp_side': perp_side,
                'spot_side': spot_side,
            },
        }
    
    def _check_exit_signal(
        self, 
        symbol: str, 
        funding_rate: float,
        position_entry_time: datetime = None,
        position_perp_side: str = None,
    ) -> Optional[Dict[str, Any]]:
        """Check if we should exit an existing arbitrage position."""
        
        # Check minimum holding period
        if position_entry_time:
            time_held = datetime.now() - position_entry_time
            min_hold_time = timedelta(hours=self.funding_interval_hours * self.min_holding_periods)
            
            if time_held < min_hold_time:
                return None
        
        # Check if funding rate has normalized or reversed
        abs_funding = abs(funding_rate)
        exit_reason = None
        
        # Exit if funding rate dropped below exit threshold
        if abs_funding < self.exit_threshold:
            exit_reason = f"Funding rate normalized to {funding_rate:.4%}"
        
        # Exit if funding rate reversed significantly (would cost us money)
        if position_perp_side:
            original_direction = 1 if position_perp_side == 'short' else -1
            current_direction = 1 if funding_rate > 0 else -1
            
            if original_direction != current_direction and abs_funding > self.exit_threshold:
                exit_reason = f"Funding rate reversed to {funding_rate:.4%}"
        
        if not exit_reason:
            return None
        
        # Determine close sides (opposite of entry)
        if position_perp_side:
            perp_close_side = 'buy' if position_perp_side == 'short' else 'sell'
            spot_close_side = 'sell' if position_perp_side == 'short' else 'buy'
        else:
            # Fallback if no position info
            perp_close_side = 'buy'
            spot_close_side = 'sell'
        
        return {
            'signal': 'sell',  # Standard signal type for exit
            'signal_type': 'multi_leg',
            'action': 'exit',
            'symbol': symbol,
            'reason': exit_reason,
            'strategy': 'funding_rate_arbitrage',
            'funding_rate': funding_rate,
            # Multi-leg specification for closing
            'legs': [
                {
                    'symbol': symbol,
                    'market_type': 'perp',
                    'side': 'close',
                    'order_side': perp_close_side,
                    'reduce_only': True,
                },
                {
                    'symbol': f"{symbol}/USDC",
                    'market_type': 'spot',
                    'side': 'close',
                    'order_side': spot_close_side,
                },
            ],
            'atomic': True,
            'urgency': 'high',  # Exits should be fast
        }
    
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
            
            if signal and signal.get('action') == 'enter':
                opportunities.append(signal)
        
        # Sort by annualized return (descending)
        opportunities.sort(key=lambda x: x.get('annualized_return', 0), reverse=True)
        
        return opportunities
    
    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: Dict[str, pd.DataFrame] = None,
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
    
    def should_exit(self, position: Any, current_price: float, 
                   current_data: Dict[str, Any] = None) -> Tuple[bool, Optional[str]]:
        """
        Determine if funding arb position should exit.
        
        Exit is based on funding rate, not price. This is handled by
        _check_exit_signal in the generate_signal flow, so we return False here
        to avoid duplicate exit logic.
        
        Multi-leg funding arb exits are managed separately.
        """
        # Funding arb exit logic is handled in _check_exit_signal 
        # which is called from generate_signal when has_existing_position=True
        # We don't use this method for funding arb
        return False, None
    
    def update_funding_cache(self, symbol: str, funding_rate: float):
        """Update the funding rate cache for a symbol."""
        if symbol not in self.funding_rate_cache:
            self.funding_rate_cache[symbol] = []
        
        self.funding_rate_cache[symbol].append((datetime.now(), funding_rate))
        
        # Keep only recent history
        max_history = self.funding_history_periods * 2
        if len(self.funding_rate_cache[symbol]) > max_history:
            self.funding_rate_cache[symbol] = self.funding_rate_cache[symbol][-max_history:]
    
    def get_funding_history(self, symbol: str) -> List[float]:
        """Get funding rate history for a symbol."""
        history = self.funding_rate_cache.get(symbol, [])
        return [r[1] for r in history[-self.funding_history_periods:]]
    def calculate_signal_strength(self, ohlcv: Dict[str, pd.DataFrame], symbol: str = None, signal_context: Dict[str, Any] = None) -> float:
        """
        Calculate signal strength based on Annualized Return (APR).
        
        Mapping:
        - APR 10% -> 0.5
        - APR 50% -> 1.0
        """
        if signal_context and 'annualized_return' in signal_context:
            apr = float(signal_context['annualized_return'])
            
            # Normalize
            base_apr = 0.10 # 10%
            max_apr = 0.50  # 50%
            
            if apr >= max_apr:
                return 1.0
            
            # Map [0.10, 0.50] -> [0.5, 1.0]
            strength = 0.5 + 0.5 * (apr - base_apr) / (max_apr - base_apr)
            return max(0.4, min(1.0, strength)) # Floor at 0.4 for valid signals
            
        return 0.5
