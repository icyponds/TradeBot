"""
Cross-Sectional Momentum Strategy.

This strategy trades based on the relative strength of assets compared to the rest of the universe.
It aims to be Market-Neutral by Longing the Top N Winners and Shorting the Bottom N Losers.

Logic:
1.  Calculate 24h Return for all assets.
2.  Rank assets by Return.
3.  Long Top Decile (e.g., Top 10%).
4.  Short Bottom Decile (e.g., Bottom 10%).
5.  Rebalance: Hourly.

Implementation Note:
Since `generate_signal` is called per-symbol, this strategy maintains a shared Class-Level 
cache of returns to determine rankings dynamically.
"""

import logging
from typing import Dict, Any, Optional, Tuple, List
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from .base_strategy import BaseStrategy

class CrossSectionalMomentumStrategy(BaseStrategy):
    """
    Cross-Sectional Momentum (L/S Neutral) Strategy.
    """
    
    PREFERRED_TIMEFRAME = '1h'
    
    # Shared State for Cross-Sectional Ranking
    # {symbol: {'return': float, 'timestamp': datetime}}
    _universe_stats: Dict[str, Dict[str, Any]] = {}
    _last_cleanup = datetime.min
    
    def __init__(self, config: Dict[str, Any], timeframe: str = None):
        super().__init__(config, timeframe)
        
        csm_config = config.get('strategies', {}).get('cross_sectional_momentum', {})
        
        self.lookback_period = csm_config.get('lookback_period', 24) # 24h Momentum
        self.top_n_percent = csm_config.get('top_n_percent', 0.10)   # Top 10%
        self.bottom_n_percent = csm_config.get('bottom_n_percent', 0.10) # Bottom 10%
        self.rebalance_interval_hours = csm_config.get('rebalance_interval', 4)
        
        self.logger.info(f"Initialized Cross-Sectional Momentum: "
                        f"Lookback={self.lookback_period}h, "
                        f"Top/Bottom={self.top_n_percent:.0%}")

    def generate_signal(self, symbol: str, ohlcv: Dict[str, pd.DataFrame]) -> Optional[Dict[str, Any]]:
        """
        Generate L/S signal based on relative rank.
        """
        data = ohlcv.get(self.timeframe)
        if data is None or len(data) < self.lookback_period:
            return None
            
        return self._generate_signal_internal(data, symbol)
        
    def _generate_signal_internal(self, ohlcv: pd.DataFrame, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Calculate return, update universe cache, and determine rank.
        """
        # 1. Update Universe Stats
        current_price = ohlcv['close'].iloc[-1]
        
        if len(ohlcv) >= self.lookback_period:
            # Calculate Momentum (Return)
            past_price = ohlcv['close'].iloc[-self.lookback_period]
            momentum = (current_price / past_price) - 1
            
            # Store in shared cache
            self._universe_stats[symbol] = {
                'return': momentum,
                'timestamp': datetime.now(),
                'volatility': ohlcv['close'].pct_change().std()
            }
        
        # 2. Clean old entries (once per cycle)
        now = datetime.now()
        if now - self._last_cleanup > timedelta(minutes=15):
             self._cleanup_cache()
             CrossSectionalMomentumStrategy._last_cleanup = now
             
        # 3. Determine Rank
        # Need enough assets to rank
        if len(self._universe_stats) < 5:
            return None
            
        returns = [v['return'] for k, v in self._universe_stats.items()]
        my_return = self._universe_stats.get(symbol, {}).get('return', 0)
        
        # Calculate Percentile (0 to 1)
        # Percentile of my_return within returns
        rank = pd.Series(returns).rank(pct=True).iloc[list(self._universe_stats.keys()).index(symbol)] if symbol in self._universe_stats else 0.5
        
        # But `list(keys()).index` is unstable if dict changes. 
        # Better: Simple comparison
        sorted_returns = sorted(returns)
        try:
             # Find approximate rank
             idx = next(i for i, x in enumerate(sorted_returns) if x >= my_return)
             rank = idx / len(sorted_returns)
        except StopIteration:
            rank = 1.0

        signal = 'hold'
        reason = ''
        
        # 4. Generate Signal
        if rank >= (1.0 - self.top_n_percent):
            # Top Winner -> Long
            signal = 'buy'
            reason = f"Cross-Sectional Mom: Top {self.top_n_percent:.0%} Winner (Rank {rank:.2f}, Ret {my_return:.1%})"
        elif rank <= self.bottom_n_percent:
            # Bottom Loser -> Short
            signal = 'sell'
            reason = f"Cross-Sectional Mom: Bottom {self.bottom_n_percent:.0%} Loser (Rank {rank:.2f}, Ret {my_return:.1%})"
            
        if signal == 'hold':
            return None
            
        # 5. Trend Filter Implementation (EMA 200)
        # Only take LONG signals if Price > EMA200
        # Only take SHORT signals if Price < EMA200
        ohlcv['ema200'] = ohlcv['close'].ewm(span=200, adjust=False).mean()
        trend_ema = ohlcv['ema200'].iloc[-1]
        
        if signal == 'buy' and current_price < trend_ema:
             self.logger.debug(f"{symbol}: Long signal filtered (Price {current_price:.2f} < EMA200 {trend_ema:.2f})")
             return None
             
        if signal == 'sell' and current_price > trend_ema:
             self.logger.debug(f"{symbol}: Short signal filtered (Price {current_price:.2f} > EMA200 {trend_ema:.2f})")
             return None

        # Volatility Targeting for Size?
        # Higher Vol -> Smaller Size (managed by position sizing logic, but we can signal confidence)
        confidence = abs(rank - 0.5) * 2 # 0.5 -> 0, 1.0 -> 1.0
        
        return {
            'signal': signal,
            'reason': reason + f" [Trend Filter: {'Above' if current_price > trend_ema else 'Below'} EMA200]",
            'price': current_price,
            'strategy': 'cross_sectional_momentum',
            'confidence': confidence,
            'rank': rank,
            'momentum': my_return
        }

    @classmethod
    def _cleanup_cache(cls):
        """Remove stale entries (> 4h old) from universe stats."""
        # Updated to 4h since we rebalance daily, but data refreshes hourly.
        cutoff = datetime.now() - timedelta(hours=4)
        to_remove = [k for k, v in cls._universe_stats.items() if v['timestamp'] < cutoff]
        for k in to_remove:
            del cls._universe_stats[k]

    def calculate_stop_loss(self, entry_price: float, side: str, 
                            signal_context: Dict[str, Any] = None) -> float:
        """
        Momentum trades can reverse quickly.
        Wide stop (it's a portfolio play), rely on rebalancing.
        """
        sl_pct = 0.05 # 5% max loss per leg
        
        if side == 'buy':
            return entry_price * (1 - sl_pct)
        else:
            return entry_price * (1 + sl_pct)

    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: Dict[str, pd.DataFrame] = None,
                              signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """
        Let winners run. No fixed TP, exit on Rank deterioration (Rebalance).
        """
        return 0.0 # Disabled
        
    def get_trailing_stop_config(self) -> Dict[str, Any]:
        """
        Trailing stop to capture trend collapses.
        """
        return {
            'enabled': True,
            'trail_pct': 0.04,
            'activation_pct': 0.05
        }
    def calculate_signal_strength(self, ohlcv: Dict[str, pd.DataFrame], symbol: str = None, signal_context: Dict[str, Any] = None) -> float:
        """
        Calculate signal strength based on Rank Confidence.
        
        Rank Confidence = abs(Rank - 0.5) * 2
        - Top/Bottom 1% -> ~1.0
        - Top/Bottom 10% -> ~0.8
        """
        if signal_context and 'confidence' in signal_context:
            return float(signal_context['confidence'])
            
        return 0.5
