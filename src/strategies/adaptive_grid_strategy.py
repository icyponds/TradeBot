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
from src.utils.statistics import calculate_atr, calculate_adx, calculate_rsi

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
        
        # Trend Strength Filter (Regime Gating)
        self.adx_threshold = grid_config.get('adx_threshold', 30)

        # RSI Mean Reversion Filter
        self.rsi_period = grid_config.get('rsi_period', 14)
        self.rsi_long_threshold = grid_config.get('rsi_long_threshold', 40)
        self.rsi_short_threshold = grid_config.get('rsi_short_threshold', 60)
        
        self.logger.info(f"Initialized Adaptive Grid Strategy: "
                        f"EMA={self.ema_period}, Spacing={self.grid_spacing_atr}xATR, ADX_Limit={self.adx_threshold}, "
                        f"RSI Filter=({self.rsi_long_threshold}/{self.rsi_short_threshold})")
    
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
        
        required_len = max(self.ema_period, self.atr_period, 28) + 5 # Need extra for ADX
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
        # 2. Calculate Volatility (ATR)
        atr = calculate_atr(highs, lows, closes, self.atr_period)
        current_atr = atr.iloc[-1]
        
        # 3. Checker Regime (ADX)
        # 3. Checker Regime (ADX & RSI)
        adx = calculate_adx(highs, lows, closes)
        current_adx = adx.iloc[-1]
        
        rsi = calculate_rsi(closes, self.rsi_period)
        current_rsi = rsi.iloc[-1]
        
        # REGIME GATING: If ADX > Threshold, trend is too strong for Mean Reversion grid
        if current_adx > self.adx_threshold:
            # self.logger.debug(f"Skipping grid logic for {symbol}: Trend too strong (ADX={current_adx:.1f} > {self.adx_threshold})")
            return None
        
        # 4. Define Grid Bands
        band_distance = current_atr * self.grid_spacing_atr
        
        # COST-AWARE FILTER: Ensure band distance covers fees + min profit
        # Estimated round-trip fees ~0.06% to 0.1%, slippage ~0.05%
        # Target min profit 0.2%
        min_distance_pct = 0.003
        if (band_distance / current_price) < min_distance_pct:
            band_distance = current_price * min_distance_pct
        
        upper_band = current_ema + band_distance
        lower_band = current_ema - band_distance
        
        signal = 'hold'
        reason = ''
        
        # 5. Generate Mean Reversion Signals
        # Entry Long: Price dips below Lower Band (oversold relative to trend) AND RSI is oversold
        if current_price < lower_band:
            if current_rsi < self.rsi_long_threshold:
                signal = 'buy'
                reason = f"Adaptive Grid: Buy Dip (Price {current_price} < {lower_band:.2f}, RSI={current_rsi:.1f} < {self.rsi_long_threshold})"
            else:
                self.logger.debug(f"{symbol}: Grid Long skipped (RSI {current_rsi:.1f} >= {self.rsi_long_threshold})")
            
        # Entry Short: Price spikes above Upper Band (overbought relative to trend) AND RSI is overbought
        elif current_price > upper_band:
            if current_rsi > self.rsi_short_threshold:
                signal = 'sell'
                reason = f"Adaptive Grid: Sell Pump (Price {current_price} > {upper_band:.2f}, RSI={current_rsi:.1f} > {self.rsi_short_threshold})"
            else:
                self.logger.debug(f"{symbol}: Grid Short skipped (RSI {current_rsi:.1f} <= {self.rsi_short_threshold})")
        
        if signal == 'hold':
            return None
            
        return {
            'signal': signal,
            'reason': reason,
            'price': current_price,
            'strategy': 'adaptive_grid',
            'ema': current_ema,
            'atr': current_atr,
            'adx': current_adx,
            'deviation_ratio': abs(current_price - current_ema) / (current_atr * self.grid_spacing_atr) if (current_atr * self.grid_spacing_atr) > 0 else 0
        }



    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: Dict[str, pd.DataFrame] = None,
                             signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """
        Calculate Take Profit.
        Target is roughly 1.0 ATR away (Mean Reversion to EMA).
        """
        # Calculate dynamic ATR if data available
        tp_dist = entry_price * 0.02 # Default fallback
        
        try:
            if ohlcv:
                tf_data = ohlcv.get(self.timeframe) or next(iter(ohlcv.values()))
                if len(tf_data) >= 20:
                    high = tf_data['high']
                    low = tf_data['low']
                    close = tf_data['close']
                    current_atr = calculate_atr(high, low, close, 14).iloc[-1]
                    
                    # Target 1.0 ATR profit
                    tp_dist = current_atr
                    
                    # Ensure it's at least min profit
                    if tp_dist < entry_price * 0.004:
                         tp_dist = entry_price * 0.004
        except Exception:
            pass
            
        if side == 'buy':
            return entry_price + tp_dist
        else:
            return entry_price - tp_dist

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

    def calculate_signal_strength(self, ohlcv: Dict[str, pd.DataFrame], symbol: str = None, signal_context: Dict[str, Any] = None) -> float:
        """
        Calculate signal strength based on Grid Deviation.
        
        Mapping:
        - Ratio 1.0 (Entry) -> 0.5
        - Ratio 2.0 (Double Spacing) -> 1.0
        """
        ratio = 0.0
        if signal_context and 'deviation_ratio' in signal_context:
            ratio = float(signal_context['deviation_ratio'])
            
        if ratio < 1.0:
            return 0.5
            
        max_ratio = 2.0
        if ratio >= max_ratio:
            return 1.0
            
        return 0.5 + 0.5 * (ratio - 1.0) / (max_ratio - 1.0)
