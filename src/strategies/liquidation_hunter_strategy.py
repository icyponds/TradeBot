
"""
Liquidation Hunter Strategy.

This strategy aims to capture "wick" reversals caused by forced liquidations.
In the absence of a real-time liquidation feed (backtest mode), it uses statistical
outliers (extreme Bollinger Band excursions) as proxy signals for liquidation cascades.

Logic:
1. Detect Extreme Excursion: Price > SMA + K * StdDev (where K is typically 3.0+).
2. Entry: Fade the move (Counter-trend) immediately.
3. Exit: Fast mean reversion (exit at SMA or minimal profit).
4. Risk: Very tight stops, as catching a falling knife is dangerous if momentum continues.
"""

import logging
from typing import Dict, Any, Optional, Tuple, List
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy

class LiquidationHunterStrategy(BaseStrategy):
    """
    Liquidation Hunter / Extreme Mean Reversion Strategy.
    """
    
    # Needs fast reaction
    PREFERRED_TIMEFRAME = '5m'
    
    def __init__(self, config: Dict[str, Any], timeframe: str = None):
        super().__init__(config, timeframe)
        
        # Strategy parameters
        hunter_config = config.get('strategies', {}).get('liquidation_hunter', {})
        
        # Outlier Detection settings
        self.bollinger_window = hunter_config.get('window', 20)
        self.std_dev_threshold = hunter_config.get('std_dev_threshold', 3.5) # Tuned from 3.0 to 3.5
        
        self.logger.info(f"Initialized Liquidation Hunter: "
                        f"Sigma={self.std_dev_threshold}, Window={self.bollinger_window}")
    
    def generate_signal(self, symbol: str, ohlcv: Dict[str, pd.DataFrame]) -> Optional[Dict[str, Any]]:
        """
        Generate mean reversion signal on extremes.
        """
        tf_data = self._get_timeframe_data(ohlcv)
        if tf_data is None:
            return None
        
        return self._generate_signal_internal(tf_data, symbol)
    
    def _generate_signal_internal(self, ohlcv: pd.DataFrame, symbol: str) -> Optional[Dict[str, Any]]:
        """Internal logic."""
        
        if len(ohlcv) < self.bollinger_window + 5:
            return None
        
        closes = ohlcv['close']
        current_price = closes.iloc[-1]
        
        # Calculate Rolling Stats
        ma = closes.rolling(window=self.bollinger_window).mean()
        std = closes.rolling(window=self.bollinger_window).std()
        
        current_ma = ma.iloc[-1]
        current_std = std.iloc[-1]
        
        if current_std == 0:
            return None
            
        # Z-Score of current price relative to MA
        z_score = (current_price - current_ma) / current_std
        
        signal = 'hold'
        reason = ''
        
        # Calculate Wick Ratios to confirm reversal/exhaustion
        # Avoid entering on full-body candles which imply continuation
        high = ohlcv['high'].iloc[-1]
        low = ohlcv['low'].iloc[-1]
        close = ohlcv['close'].iloc[-1]
        op = ohlcv['open'].iloc[-1] # 'open' is keyword
        
        candle_range = high - low
        if candle_range == 0:
            candle_range = 1e-9
            
        # Buy Signal: Look for wick at bottom (Close > Low)
        # Wick Ratio = (Close - Low) / Range. 
        # If 0.0, we closed at Low (Max Bearish). If 1.0, we closed at High.
        # We want meaningful bounce from low -> Wick > 0.1
        buy_wick_ratio = (close - low) / candle_range
        
        # Sell Signal: Look for wick at top (High - Close)
        # Sell Wick Ratio = (High - Close) / Range.
        # If 0.0, we closed at High (Max Bullish).
        sell_wick_ratio = (high - close) / candle_range
        
        wick_threshold = 0.15 # Require 15% bounce off extreme
        
        # Entry Logic: Fade Extreme Moves WITH Wick Confirmation
        if z_score > self.std_dev_threshold:
            # Price exploded upwards > 3.5 sigma
            # Require rejection from highs (Sell Wick)
            if sell_wick_ratio >= wick_threshold:
                signal = 'sell'
                reason = f"Liquidation Hunter: Price +{z_score:.2f}σ Excursion (Short Wick {sell_wick_ratio:.2f})"
            else:
                 # It's a full green candle. Wait.
                 pass
            
        elif z_score < -self.std_dev_threshold:
            # Price crashed downwards < -3.5 sigma
            # Require bounce from lows (Buy Wick)
            if buy_wick_ratio >= wick_threshold:
                signal = 'buy'
                reason = f"Liquidation Hunter: Price {z_score:.2f}σ Excursion (Long Wick {buy_wick_ratio:.2f})"
            else:
                # It's a full red candle. Wait.
                pass
            
        if signal == 'hold':
            return None
            
        return {
            'signal': signal,
            'reason': reason,
            'price': current_price,
            'strategy': 'liquidation_hunter',
            'z_score': z_score,
            'mean': current_ma
        }
    
    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: Dict[str, pd.DataFrame] = None,
                             signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """
        Take profit at Mean Reversion or partial.
        """
        # Target usually the MA. Approximation:
        # If 3 sigma out, return to 0 sigma is distinct.
        # Let's set a fixed TP for simplicity or rely on trailing/exit logic.
        
        tp_pct = 0.02 # 2% capture of wick
        
        if side == 'buy':
            return entry_price * (1 + tp_pct)
        else:
            return entry_price * (1 - tp_pct)
            
    def should_exit(self, position: Any, current_price: float, 
                   current_data: Dict[str, Any] = None) -> Tuple[bool, Optional[str]]:
        """
        Exit if price returns to mean (Z-Score near 0).
        """
        # This requires re-calculating Z-Score which isn't passed in current_data generically.
        # But we can assume if PnL is positive enough we exit.
        # Or simplistic: Exit if we crossed the SMA?
        
        # For this implementation, we rely on TP or Trailing Stop provided by engine.
        return False, None

    def get_trailing_stop_config(self) -> Dict[str, Any]:
        """
        Very tight trailing stop to secure wick profits immediately.
        """
        return {
            'enabled': True,
            'trail_pct': 0.005,      # 0.5% trailing
            'activation_pct': 0.005, # Activate immediately
        }
    def calculate_signal_strength(self, ohlcv: Dict[str, pd.DataFrame], symbol: str = None, signal_context: Dict[str, Any] = None) -> float:
        """
        Calculate signal strength based on Liquidation Z-Score.
        
        Mapping:
        - Z-Score 3.5 (Entry) -> 0.5
        - Z-Score 5.0 (Max) -> 1.0
        """
        z_score = 0.0
        if signal_context and 'z_score' in signal_context:
            z_score = abs(signal_context['z_score'])
            
        if z_score < self.std_dev_threshold:
            return 0.5
            
        z_max = 5.0
        if z_score >= z_max:
            return 1.0
            
        return 0.5 + 0.5 * (z_score - self.std_dev_threshold) / (z_max - self.std_dev_threshold)
