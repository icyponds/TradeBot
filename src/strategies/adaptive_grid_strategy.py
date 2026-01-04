"""
Adaptive Grid Strategy.

This strategy is designed to profit from market noise (volatility) around a central trend.
Unlike static grids, this "Adaptive Grid" centers itself on a moving average (EMA)
and expands/contracts its levels based on market volatility (ATR).

Logic:
1. Trend Baseline: EMA (e.g., 50-period).
2. Grid Spacing: Based on ATR (e.g., 1.0 * ATR).
3. Entry: Price deviates from Baseline by > Spacing (Mean Reversion).
4. Exit: Price returns to Baseline.
"""

import logging
from typing import Dict, Any, Optional, Tuple
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy

class AdaptiveGridStrategy(BaseStrategy):
    """
    Adaptive Grid Strategy.
    
    Trades mean reversion around a moving baseline.
    """
    
    # 15m timeframe to capture noise
    PREFERRED_TIMEFRAME = '15m'
    
    def __init__(self, config: Dict[str, Any], timeframe: str = None):
        super().__init__(config, timeframe)
        
        # Strategy parameters from config
        grid_config = config.get('strategies', {}).get('adaptive_grid', {})
        
        # Trend Baseline settings
        self.ema_period = grid_config.get('ema_period', 50)
        
        # Grid settings
        self.atr_period = grid_config.get('atr_period', 14)
        self.grid_spacing_atr = grid_config.get('grid_spacing_atr', 1.5)  # Entry at 1.5 ATR deviation
        
        # Trend Filter (optional): Only trade with slope?
        self.trend_filter_enabled = grid_config.get('trend_filter_enabled', True)
        
        self.logger.info(f"Initialized Adaptive Grid Strategy: "
                        f"EMA={self.ema_period}, Spacing={self.grid_spacing_atr}xATR")
    
    def generate_signal(self, symbol: str, ohlcv: Dict[str, pd.DataFrame]) -> Optional[Dict[str, Any]]:
        """
        Generate grid signal.
        """
        # Get preferred timeframe data
        tf_data = ohlcv.get(self.timeframe)
        if tf_data is None:
            if ohlcv:
                tf_data = next(iter(ohlcv.values()))
            else:
                return None
        
        return self._generate_signal_internal(tf_data, symbol)
    
    def _generate_signal_internal(self, ohlcv: pd.DataFrame, symbol: str) -> Optional[Dict[str, Any]]:
        """Internal signal generation logic."""
        
        required_len = max(self.ema_period, self.atr_period) + 5
        if len(ohlcv) < required_len:
            return None
        
        closes = ohlcv['close']
        highs = ohlcv['high']
        lows = ohlcv['low']
        current_price = closes.iloc[-1]
        
        # 1. Calculate Baseline (EMA)
        ema = closes.ewm(span=self.ema_period, adjust=False).mean()
        current_ema = ema.iloc[-1]
        
        # 2. Calculate Volatility (ATR)
        atr = self._calculate_atr(highs, lows, closes)
        current_atr = atr.iloc[-1]
        
        # 3. Define Grid Bands
        upper_band = current_ema + (current_atr * self.grid_spacing_atr)
        lower_band = current_ema - (current_atr * self.grid_spacing_atr)
        
        signal = 'hold'
        reason = ''
        
        # 4. Generate Mean Reversion Signals
        # Entry Long: Price dips below Lower Band (oversold relative to trend)
        if current_price < lower_band:
            # Check Trend Filter: EMA should not be falling too fast?
            # For now, simplistic grid logic: buy the dip
            signal = 'buy'
            reason = f"Adaptive Grid: Price {current_price} < Lower Band {lower_band:.2f} (EMA={current_ema:.2f})"
            
        # Entry Short: Price spikes above Upper Band (overbought relative to trend)
        elif current_price > upper_band:
            signal = 'sell'
            reason = f"Adaptive Grid: Price {current_price} > Upper Band {upper_band:.2f} (EMA={current_ema:.2f})"
        
        if signal == 'hold':
            return None
            
        return {
            'signal': signal,
            'reason': reason,
            'price': current_price,
            'strategy': 'adaptive_grid',
            'ema': current_ema,
            'atr': current_atr
        }

    def _calculate_atr(self, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
        """Calculate ATR."""
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.rolling(window=self.atr_period).mean()

    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: Dict[str, pd.DataFrame] = None,
                             signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """
        Calculate Take Profit.
        Target is the EMA (Mean Reversion).
        """
        # We don't have access to current EMA here easily without recalculating
        # So we use a relative target based on the spacing (1.5 ATR entry -> target ~1.5 ATR away)
        # Using a conservative 1.0 ATR distance as TP
        
        # Approx ATR from volatility or context?
        # Fallback to percentage
        tp_dist = entry_price * 0.02 # 2% default
        
        if side == 'buy':
            return entry_price + tp_dist
        else:
            return entry_price - tp_dist

    def calculate_stop_loss(self, entry_price: float, side: str, 
                           signal_context: Dict[str, Any] = None) -> float:
        """
        Calculate Stop Loss.
        Grid strategies often run without stops or wide stops.
        Active management (Adaptivity) is key.
        We'll use a wide stop (e.g., 3x Grid Spacing).
        """
        sl_dist = entry_price * 0.05  # 5% wide stop
        
        if side == 'buy':
            return entry_price - sl_dist
        else:
            return entry_price + sl_dist
            
    def get_trailing_stop_config(self) -> Dict[str, Any]:
        """
        Grid strategies typically take fixed profit at levels.
        But a tight trail can secure profit if it pumps.
        """
        return {
            'enabled': True,
            'trail_pct': 0.015,        # 1.5% trail
            'activation_pct': 0.01,    # Activate after 1% gain
        }

    def should_exit(self, position: Any, current_price: float, 
                   current_data: Dict[str, Any] = None) -> Tuple[bool, Optional[str]]:
        """
        Exit if price returns to EMA?
        """
        # This requires passing EMA in current_data or recalculating.
        # StrategyManager passes minimal data.
        # For now, rely on Take Profit being set effectively.
        return False, None
