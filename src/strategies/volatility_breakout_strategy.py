"""
Volatility Breakout Strategy.

This strategy captures explosive moves that rarely occur but offer high reward-to-risk ratios.
It identifies periods of market consolidation ("squeezes") and enters when price breaks out
with expanding volatility.

Logic:
1. Identify Squeeze: Bollinger Band Width < Threshold (low volatility).
2. Signal Breakout: Price closes above Upper Band (Long) or below Lower Band (Short).
3. Confirmation: Volume expansion (optional but recommended).
4. Exit: ATR-based trailing stop or mean reversion to Moving Average.
"""

import logging
from typing import Dict, Any, Optional, Tuple, List
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy

class VolatilityBreakoutStrategy(BaseStrategy):
    """
    Volatility Breakout Strategy (Bollinger Band Squeeze).
    
    Captures trends starting from consolidation zones.
    """
    
    # 1h timeframe is ideal for capturing multi-hour/day moves
    PREFERRED_TIMEFRAME = '1h'
    
    def __init__(self, config: Dict[str, Any], timeframe: str = None):
        super().__init__(config, timeframe)
        
        # Strategy parameters from config
        vb_config = config.get('strategies', {}).get('volatility_breakout', {})
        
        # Bollinger Band settings
        self.bb_length = vb_config.get('bb_length', 20)
        self.bb_std = vb_config.get('bb_std', 2.0)
        
        # Squeeze detection: Bandwidth must be below this threshold to qualify as "squeeze"
        # Bandwidth = (Upper - Lower) / Middle
        # Low value (e.g., 0.05 or 0.10) implies tight consolidation
        self.squeeze_threshold = vb_config.get('squeeze_threshold', 0.10)
        
        # ATR Trailing Stop settings
        self.atr_length = vb_config.get('atr_length', 14)
        self.atr_multiplier_sl = vb_config.get('atr_multiplier_sl', 2.0)   # Initial Stop Loss
        self.atr_multiplier_tp = vb_config.get('atr_multiplier_tp', 4.0)   # Take Profit (optional)
        
        # State: Track if we are currently in or recently exited a squeeze
        # Map symbol -> squeeze_active (bool)
        self.squeeze_state: Dict[str, bool] = {}
        
        self.logger.info(f"Initialized Volatility Breakout Strategy: "
                        f"BB({self.bb_length},{self.bb_std}), Squeeze<{self.squeeze_threshold}")
    
    def generate_signal(self, symbol: str, ohlcv: Dict[str, pd.DataFrame]) -> Optional[Dict[str, Any]]:
        """
        Generate breakout signal.
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
        
        if len(ohlcv) < max(self.bb_length, self.atr_length) + 5:
            return None
        
        closes = ohlcv['close']
        highs = ohlcv['high']
        lows = ohlcv['low']
        current_price = closes.iloc[-1]
        
        # 1. Calculate Bollinger Bands
        bb_upper, bb_middle, bb_lower = self._calculate_bollinger_bands(closes)
        
        # 2. Calculate Bandwidth
        # Bandwidth = (Upper - Lower) / Middle
        bandwidth = (bb_upper - bb_lower) / bb_middle
        current_bandwidth = bandwidth.iloc[-1]
        
        # 3. Check for Squeeze (Consolidation)
        is_squeeze = current_bandwidth < self.squeeze_threshold
        
        # Update state (we allow breakout if squeeze is active OR was active very recently)
        #Ideally we want entrance exactly when squeeze "fires" (expands)
        prev_is_squeeze = bandwidth.iloc[-2] < self.squeeze_threshold
        
        # 4. Check for Breakout
        # Breakout LONG: Close > Upper Band
        # Breakout SHORT: Close < Lower Band
        
        # We only take the trade if:
        # A) We were in a squeeze recently (expansion phase)
        # B) The breakout is fresh (occurred on this candle)
        valid_setup = is_squeeze or prev_is_squeeze
        
        # 5. Regime Filter: Hurst Exponent (Trend Checking)
        # We only want to enter breakouts if the market is in a Trending Regime (H > 0.5)
        # Calculating on closing prices
        hurst = self._calculate_hurst_exponent(closes)
        valid_regime = hurst > 0.5
        
        signal = 'hold'
        reason = ''
        
        if valid_setup and valid_regime:
            if current_price > bb_upper.iloc[-1]:
                signal = 'buy'
                reason = f"Volatility Breakout: Price {current_price} > Upper BB (BW={current_bandwidth:.3f}, H={hurst:.2f})"
            elif current_price < bb_lower.iloc[-1]:
                signal = 'sell'
                reason = f"Volatility Breakout: Price {current_price} < Lower BB (BW={current_bandwidth:.3f}, H={hurst:.2f})"
        
        if signal == 'hold':
            return None
            
        # Calculate ATR for dynamic stops
        atr = self._calculate_atr(highs, lows, closes)
        current_atr = atr.iloc[-1]
        
        return {
            'signal': signal,
            'reason': reason,
            'price': current_price,
            'strategy': 'volatility_breakout',
            'atr': current_atr,
            'bandwidth': current_bandwidth,
            'hurst': hurst
        }

    def _calculate_hurst_exponent(self, ts: pd.Series) -> float:
        """
        Calculate the Hurst Exponent of a time series (R/S Analysis).
        
        H < 0.5: Mean reverting
        H = 0.5: Random walk
        H > 0.5: Trending
        """
        try:
            ts = ts.values
            if len(ts) < 100:
                # Need sufficient data for Hurst
                return 0.5
                
            lags = range(2, 20)
            tau = []
            
            # Calculate standard deviation of differences
            for lag in lags:
                # Difference between t and t-lag
                diff = np.subtract(ts[lag:], ts[:-lag])
                tau.append(np.std(diff))
            
            # Fit line to log-log plot to get H (slope)
            # log(std(diff)) = H * log(lag) + C
            m = np.polyfit(np.log(lags), np.log(tau), 1)
            hurst = m[0]
            
            return hurst
            
        except Exception:
            # Fallback
            return 0.5

    def _calculate_bollinger_bands(self, series: pd.Series) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """Calculate Bollinger Bands."""
        middle = series.rolling(window=self.bb_length).mean()
        std = series.rolling(window=self.bb_length).std()
        upper = middle + (self.bb_std * std)
        lower = middle - (self.bb_std * std)
        return upper, middle, lower

    def _calculate_atr(self, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
        """Calculate ATR."""
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.rolling(window=self.atr_length).mean()

    def calculate_stop_loss(self, entry_price: float, side: str, 
                           signal_context: Dict[str, Any] = None) -> float:
        """Calculate ATR-based stop loss."""
        # Default fallback
        atr_sl_dist = entry_price * 0.05
        
        if signal_context and 'atr' in signal_context:
            atr = signal_context['atr']
            if atr > 0:
                atr_sl_dist = atr * self.atr_multiplier_sl
        
        if side == 'buy':
            return entry_price - atr_sl_dist
        else:
            return entry_price + atr_sl_dist

    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: Dict[str, pd.DataFrame] = None,
                             signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """
        Calculate Take Profit.
        Prefer letting winners run with trailing stop, but define a wide target.
        """
        # Default fallback (wide target)
        tp_dist = entry_price * 0.10
        
        # If we have logic for risk:reward, we can use it here
        # But this strategy relies heavily on trailing stops
        
        if side == 'buy':
            return entry_price + tp_dist
        else:
            return entry_price - tp_dist
            
    def get_trailing_stop_config(self) -> Dict[str, Any]:
        """
        Get trailing stop configuration.
        Breakout strategies need to lock in profits once the move extends.
        """
        return {
            'enabled': True,
            'trail_pct': 0.03,         # 3% trailing stop (tight-ish for breakouts)
            'activation_pct': 0.02,    # Activate quickly after 2% gain
        }

    def should_exit(self, position: Any, current_price: float, 
                   current_data: Dict[str, Any] = None) -> Tuple[bool, Optional[str]]:
        """
        Exit logic beyond stops:
        Exit if price falls back inside the Bollinger Bands?
        For now, rely on trailing stops.
        """
        return False, None
