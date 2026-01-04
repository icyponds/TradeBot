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
        
        # Trend Strength Filter (Regime Gating)
        self.adx_threshold = grid_config.get('adx_threshold', 30)
        
        self.logger.info(f"Initialized Adaptive Grid Strategy: "
                        f"EMA={self.ema_period}, Spacing={self.grid_spacing_atr}xATR, ADX_Limit={self.adx_threshold}")
    
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
        atr = self._calculate_atr(highs, lows, closes)
        current_atr = atr.iloc[-1]
        
        # 3. Checker Regime (ADX)
        adx = self._calculate_adx(highs, lows, closes)
        current_adx = adx.iloc[-1]
        
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
        # Entry Long: Price dips below Lower Band (oversold relative to trend)
        if current_price < lower_band:
            # Check Trend Filter: EMA should not be falling too fast?
            # For now, simplistic grid logic: buy the dip
            signal = 'buy'
            reason = f"Adaptive Grid: Price {current_price} < Lower Band {lower_band:.2f} (EMA={current_ema:.2f}, ADX={current_adx:.1f})"
            
        # Entry Short: Price spikes above Upper Band (overbought relative to trend)
        elif current_price > upper_band:
            signal = 'sell'
            reason = f"Adaptive Grid: Price {current_price} > Upper Band {upper_band:.2f} (EMA={current_ema:.2f}, ADX={current_adx:.1f})"
        
        if signal == 'hold':
            return None
            
        return {
            'signal': signal,
            'reason': reason,
            'price': current_price,
            'strategy': 'adaptive_grid',
            'ema': current_ema,
            'atr': current_atr,
            'adx': current_adx
        }

    def _calculate_atr(self, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
        """Calculate ATR."""
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.rolling(window=self.atr_period).mean()

    def _calculate_adx(self, high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
        """Calculate ADX (Average Directional Index)."""
        # +DM, -DM
        up = high - high.shift(1)
        down = low.shift(1) - low
        
        plus_dm = np.where((up > down) & (up > 0), up, 0.0)
        minus_dm = np.where((down > up) & (down > 0), down, 0.0)
        
        plus_dm = pd.Series(plus_dm, index=high.index)
        minus_dm = pd.Series(minus_dm, index=high.index)
        
        # TR
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        # Smooth (Wilder's)
        # Using EWM with com = period - 1 to approximate Wilder's MMA
        tr_smooth = tr.ewm(com=period-1, min_periods=period).mean()
        plus_dm_smooth = plus_dm.ewm(com=period-1, min_periods=period).mean()
        minus_dm_smooth = minus_dm.ewm(com=period-1, min_periods=period).mean()
        
        # +DI, -DI
        plus_di = 100 * (plus_dm_smooth / tr_smooth)
        minus_di = 100 * (minus_dm_smooth / tr_smooth)
        
        # DX
        sum_di = plus_di + minus_di
        dx = 100 * abs(plus_di - minus_di) / sum_di
        
        # ADX
        adx = dx.ewm(com=period-1, min_periods=period).mean()
        
        return adx

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
                    tr1 = high - low
                    tr2 = abs(high - close.shift())
                    tr3 = abs(low - close.shift())
                    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                    current_atr = tr.rolling(window=14).mean().iloc[-1]
                    
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
