"""
Cross-Sectional Momentum Factor Strategy.

This strategy implements a classic factor-based approach that ranks assets
by their recent returns and constructs a long-short portfolio:
- Long the top N performers (winners)
- Short the bottom N performers (losers)

This creates a market-neutral portfolio that profits from momentum persistence.
"""

import logging
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy


@dataclass
class MomentumRanking:
    """Ranking of assets by momentum."""
    symbol: str
    return_pct: float
    rank: int
    percentile: float
    position: str  # 'long', 'short', or 'neutral'
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'return_pct': round(self.return_pct, 4),
            'rank': self.rank,
            'percentile': round(self.percentile, 2),
            'position': self.position,
        }


@dataclass
class MomentumPortfolio:
    """Current momentum portfolio state."""
    long_positions: List[str] = field(default_factory=list)
    short_positions: List[str] = field(default_factory=list)
    rankings: Dict[str, MomentumRanking] = field(default_factory=dict)
    last_rebalance: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'long_positions': self.long_positions,
            'short_positions': self.short_positions,
            'total_positions': len(self.long_positions) + len(self.short_positions),
            'last_rebalance': self.last_rebalance.isoformat() if self.last_rebalance else None,
        }


class MomentumFactorStrategy(BaseStrategy):
    """
    Cross-Sectional Momentum Factor Strategy.
    
    This strategy exploits the momentum anomaly - assets that have performed
    well recently tend to continue performing well (and vice versa).
    
    Strategy Logic:
        1. Calculate momentum (returns) for all assets over lookback period
        2. Rank assets by momentum
        3. Long top N performers (winners)
        4. Short bottom N performers (losers)
        5. Rebalance periodically (daily/weekly)
    
    Market Neutrality:
        - Equal dollar exposure on long and short sides
        - Net beta approximately zero
    
    Parameters:
        - lookback_days: Period for calculating momentum (e.g., 7 days)
        - top_n: Number of top performers to long
        - bottom_n: Number of bottom performers to short
        - rebalance_hours: Hours between rebalances
    """
    
    # Momentum is a slower signal - 4h timeframe balances noise vs responsiveness
    PREFERRED_TIMEFRAME = '4h'
    
    def __init__(self, config: Dict[str, Any], market_api=None):
        super().__init__(config)
        
        # Strategy parameters from config
        momentum_config = config.get('strategies', {}).get('momentum_factor', {})
        
        # Momentum calculation
        self.lookback_days = momentum_config.get('lookback_days', 7)
        self.lookback_hours = self.lookback_days * 24
        
        # Portfolio construction
        self.top_n = momentum_config.get('top_n', 3)
        self.bottom_n = momentum_config.get('bottom_n', 3)
        
        # Rebalancing
        self.rebalance_hours = momentum_config.get('rebalance_hours', 168)  # Weekly default
        
        # Minimum data requirements
        self.min_assets = momentum_config.get('min_assets', 10)
        self.min_data_points = momentum_config.get('min_data_points', 24)  # At least 24 hours
        
        # Filter thresholds
        self.min_volume_filter = momentum_config.get('min_volume_filter', 100000)  # Min 24h volume
        self.exclude_extreme_returns = momentum_config.get('exclude_extreme_returns', True)
        self.extreme_return_threshold = momentum_config.get('extreme_return_threshold', 0.5)  # 50%
        
        # Market API for data access
        self.market_api = market_api
        
        # Current portfolio state
        self.portfolio = MomentumPortfolio()
        
        # Momentum scores cache
        self.momentum_cache: Dict[str, Tuple[float, datetime]] = {}
        self.cache_ttl_hours = momentum_config.get('cache_ttl_hours', 1)
        
        self.logger.info(f"Initialized Momentum Factor Strategy: "
                        f"lookback={self.lookback_days}d, top_n={self.top_n}, "
                        f"bottom_n={self.bottom_n}, rebalance={self.rebalance_hours}h")
    
    def set_market_api(self, market_api):
        """Set the market API for data access."""
        self.market_api = market_api
    
    def generate_signal(self, ohlcv: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        Generate signal - not used for this cross-sectional strategy.
        
        This strategy operates on multiple assets simultaneously.
        Use generate_portfolio_signals instead.
        """
        return None
    
    def generate_portfolio_signals(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """
        Generate trading signals for the entire portfolio.
        
        Args:
            symbols: List of symbols to consider for the portfolio
            
        Returns:
            List of signal dictionaries for each required trade
        """
        if not self.market_api:
            self.logger.error("Market API not set")
            return []
        
        # Check if rebalance is needed
        if not self._should_rebalance():
            return []
        
        self.logger.info(f"Running momentum portfolio rebalance for {len(symbols)} symbols")
        
        # Calculate momentum for all symbols
        momentum_scores = self._calculate_all_momentum(symbols)
        
        if len(momentum_scores) < self.min_assets:
            self.logger.warning(f"Insufficient assets with momentum data: "
                              f"{len(momentum_scores)} < {self.min_assets}")
            return []
        
        # Rank and select portfolio
        rankings = self._rank_assets(momentum_scores)
        new_long, new_short = self._select_portfolio(rankings)
        
        # Generate rebalance signals
        signals = self._generate_rebalance_signals(new_long, new_short, rankings)
        
        # Update portfolio state
        self.portfolio.long_positions = new_long
        self.portfolio.short_positions = new_short
        self.portfolio.rankings = {r.symbol: r for r in rankings}
        self.portfolio.last_rebalance = datetime.now()
        
        self.logger.info(f"Momentum rebalance complete: "
                        f"Long {new_long}, Short {new_short}")
        
        return signals
    
    def _should_rebalance(self) -> bool:
        """Check if portfolio rebalance is needed."""
        if self.portfolio.last_rebalance is None:
            return True
        
        time_since_rebalance = datetime.now() - self.portfolio.last_rebalance
        return time_since_rebalance.total_seconds() / 3600 >= self.rebalance_hours
    
    def _calculate_all_momentum(self, symbols: List[str]) -> Dict[str, float]:
        """
        Calculate momentum (returns) for all symbols.
        
        Args:
            symbols: List of symbols
            
        Returns:
            Dictionary mapping symbol to momentum score
        """
        momentum_scores = {}
        
        for symbol in symbols:
            try:
                # Check cache first
                if symbol in self.momentum_cache:
                    cached_score, cache_time = self.momentum_cache[symbol]
                    age_hours = (datetime.now() - cache_time).total_seconds() / 3600
                    if age_hours < self.cache_ttl_hours:
                        momentum_scores[symbol] = cached_score
                        continue
                
                # Fetch data and calculate momentum
                momentum = self._calculate_momentum(symbol)
                
                if momentum is not None:
                    # Apply filters
                    if self.exclude_extreme_returns:
                        if abs(momentum) > self.extreme_return_threshold:
                            self.logger.debug(f"{symbol}: Excluded (extreme return {momentum:.2%})")
                            continue
                    
                    momentum_scores[symbol] = momentum
                    self.momentum_cache[symbol] = (momentum, datetime.now())
                    
            except Exception as e:
                self.logger.error(f"Error calculating momentum for {symbol}: {e}")
        
        return momentum_scores
    
    def _calculate_momentum(self, symbol: str) -> Optional[float]:
        """
        Calculate momentum for a single symbol.
        
        Momentum = (Current Price - Price N days ago) / Price N days ago
        
        Args:
            symbol: Trading symbol
            
        Returns:
            Momentum (return) as decimal or None
        """
        try:
            # Get hourly data for lookback period
            ohlcv = self.market_api.get_ohlcv(symbol, '1h', self.lookback_hours + 10)
            
            if ohlcv is None or len(ohlcv) < self.min_data_points:
                return None
            
            prices = ohlcv['close']
            current_price = prices.iloc[-1]
            
            # Get price from lookback period ago
            lookback_idx = min(self.lookback_hours, len(prices) - 1)
            past_price = prices.iloc[-lookback_idx]
            
            if past_price <= 0:
                return None
            
            # Calculate return
            momentum = (current_price - past_price) / past_price
            
            return momentum
            
        except Exception as e:
            self.logger.error(f"Error calculating momentum for {symbol}: {e}")
            return None
    
    def _rank_assets(self, momentum_scores: Dict[str, float]) -> List[MomentumRanking]:
        """
        Rank assets by momentum score.
        
        Args:
            momentum_scores: Dictionary of symbol -> momentum
            
        Returns:
            List of MomentumRanking sorted by momentum (descending)
        """
        # Sort by momentum (descending)
        sorted_assets = sorted(momentum_scores.items(), key=lambda x: x[1], reverse=True)
        n_assets = len(sorted_assets)
        
        rankings = []
        for rank, (symbol, momentum) in enumerate(sorted_assets, 1):
            percentile = (n_assets - rank + 1) / n_assets * 100
            
            # Determine position based on rank
            if rank <= self.top_n:
                position = 'long'
            elif rank > n_assets - self.bottom_n:
                position = 'short'
            else:
                position = 'neutral'
            
            rankings.append(MomentumRanking(
                symbol=symbol,
                return_pct=momentum,
                rank=rank,
                percentile=percentile,
                position=position,
            ))
        
        return rankings
    
    def _select_portfolio(self, rankings: List[MomentumRanking]) -> Tuple[List[str], List[str]]:
        """
        Select long and short positions from rankings.
        
        Args:
            rankings: Sorted list of MomentumRanking
            
        Returns:
            Tuple of (long_symbols, short_symbols)
        """
        long_positions = [r.symbol for r in rankings if r.position == 'long']
        short_positions = [r.symbol for r in rankings if r.position == 'short']
        
        return long_positions, short_positions
    
    def _generate_rebalance_signals(self, new_long: List[str], new_short: List[str],
                                   rankings: List[MomentumRanking]) -> List[Dict[str, Any]]:
        """
        Generate signals for portfolio rebalance.
        
        Args:
            new_long: New long positions
            new_short: New short positions
            rankings: Full rankings list
            
        Returns:
            List of signal dictionaries
        """
        signals = []
        
        # Get rankings dict for easy lookup
        rankings_dict = {r.symbol: r for r in rankings}
        
        # Close positions that are no longer in the portfolio
        for symbol in self.portfolio.long_positions:
            if symbol not in new_long:
                signals.append({
                    'signal': 'sell',
                    'symbol': symbol,
                    'reason': f'Momentum rebalance: {symbol} dropped from top {self.top_n}',
                    'strategy': 'momentum_factor',
                    'action': 'close_long',
                })
        
        for symbol in self.portfolio.short_positions:
            if symbol not in new_short:
                signals.append({
                    'signal': 'buy',
                    'symbol': symbol,
                    'reason': f'Momentum rebalance: {symbol} exited bottom {self.bottom_n}',
                    'strategy': 'momentum_factor',
                    'action': 'close_short',
                })
        
        # Open new long positions
        for symbol in new_long:
            if symbol not in self.portfolio.long_positions:
                ranking = rankings_dict.get(symbol)
                signals.append({
                    'signal': 'buy',
                    'symbol': symbol,
                    'reason': f'Momentum long: {symbol} rank #{ranking.rank} ({ranking.return_pct:.2%})',
                    'strategy': 'momentum_factor',
                    'action': 'open_long',
                    'momentum': ranking.return_pct if ranking else 0,
                    'rank': ranking.rank if ranking else 0,
                })
        
        # Open new short positions
        for symbol in new_short:
            if symbol not in self.portfolio.short_positions:
                ranking = rankings_dict.get(symbol)
                signals.append({
                    'signal': 'sell',
                    'symbol': symbol,
                    'reason': f'Momentum short: {symbol} rank #{ranking.rank} ({ranking.return_pct:.2%})',
                    'strategy': 'momentum_factor',
                    'action': 'open_short',
                    'momentum': ranking.return_pct if ranking else 0,
                    'rank': ranking.rank if ranking else 0,
                })
        
        return signals
    
    def get_current_rankings(self) -> List[Dict[str, Any]]:
        """Get current momentum rankings."""
        return [r.to_dict() for r in sorted(
            self.portfolio.rankings.values(),
            key=lambda x: x.rank
        )]
    
    def get_portfolio_summary(self) -> Dict[str, Any]:
        """Get summary of current portfolio state."""
        summary = self.portfolio.to_dict()
        
        # Add momentum stats
        if self.portfolio.rankings:
            momentums = [r.return_pct for r in self.portfolio.rankings.values()]
            summary['momentum_stats'] = {
                'mean': round(np.mean(momentums), 4),
                'std': round(np.std(momentums), 4),
                'max': round(max(momentums), 4),
                'min': round(min(momentums), 4),
            }
            
            # Long vs short spread
            long_momentums = [self.portfolio.rankings[s].return_pct 
                            for s in self.portfolio.long_positions 
                            if s in self.portfolio.rankings]
            short_momentums = [self.portfolio.rankings[s].return_pct 
                             for s in self.portfolio.short_positions 
                             if s in self.portfolio.rankings]
            
            if long_momentums and short_momentums:
                summary['long_short_spread'] = round(
                    np.mean(long_momentums) - np.mean(short_momentums), 4
                )
        
        return summary
    
    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: pd.DataFrame = None,
                             signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """
        Calculate take profit for momentum positions.
        
        Momentum positions are held until rebalance, so we use wide take profit.
        """
        # Wide take profit since we exit on rebalance, not price targets
        base_tp_pct = 0.15  # 15% - wide target
        
        if side == 'buy':
            return entry_price * (1 + base_tp_pct)
        else:
            return entry_price * (1 - base_tp_pct)
    
    def force_rebalance(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """
        Force a portfolio rebalance regardless of timing.
        
        Args:
            symbols: List of symbols to consider
            
        Returns:
            List of rebalance signals
        """
        # Reset last rebalance to force recalculation
        self.portfolio.last_rebalance = None
        return self.generate_portfolio_signals(symbols)
    
    def get_next_rebalance_time(self) -> Optional[datetime]:
        """Get the next scheduled rebalance time."""
        if self.portfolio.last_rebalance is None:
            return datetime.now()
        
        return self.portfolio.last_rebalance + timedelta(hours=self.rebalance_hours)

