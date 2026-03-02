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
from src.utils.statistics import hurst_exponent, calculate_atr, calculate_bollinger_bands

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
        
        # Regime filter: minimum Hurst exponent to enter (0.5 = random walk)
        self.min_hurst = vb_config.get('min_hurst', 0.5)
        
        # Time decay: exit stagnant breakouts after N hours if not in profit
        self.time_decay_hours = vb_config.get('time_decay_hours', 16)
        
        self.logger.info(f"Initialized Volatility Breakout Strategy: "
                        f"BB({self.bb_length},{self.bb_std}), Squeeze<{self.squeeze_threshold}, "
                        f"Hurst>{self.min_hurst}, TimeDecay={self.time_decay_hours}h")
    
    def generate_signal(self, symbol: str, ohlcv: Dict[str, pd.DataFrame]) -> Optional[Dict[str, Any]]:
        """
        Generate breakout signal.
        """
        tf_data = self._get_timeframe_data(ohlcv)
        if tf_data is None:
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
        bb_upper, bb_middle, bb_lower = calculate_bollinger_bands(closes, self.bb_length, self.bb_std)
        
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
        hurst = hurst_exponent(closes)
        valid_regime = hurst > self.min_hurst
        
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
        atr = calculate_atr(highs, lows, closes, self.atr_length)
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

    def calculate_stop_loss(self, entry_price: float, side: str, signal_context: Dict[str, Any] = None) -> float:
        """
        Calculate Stop Loss based on Average True Range (ATR).
        Requires 'atr' to be passed in signal_context, or uses a default fallback.
        """
        atr = None
        if signal_context and 'atr' in signal_context:
            atr = signal_context['atr']
        
        # Fallback if ATR is missing from context
        if atr is None or atr <= 0:
            atr = entry_price * 0.02  # Default to 2% if missing
            
        sl_dist = atr * self.atr_multiplier_sl
        
        if side == 'long':
            return entry_price - sl_dist
        else:
            return entry_price + sl_dist



    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: Dict[str, pd.DataFrame] = None,
                             signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """
        Calculate Take Profit based on Average True Range (ATR).
        Aligns reward expectations with the actual market volatility.
        """
        tp_dist = entry_price * 0.10  # Default fallback
        
        if ohlcv is not None:
            try:
                tf_data = self._get_timeframe_data(ohlcv)
                if tf_data is not None and len(tf_data) > self.atr_length:
                    atr = calculate_atr(tf_data['high'], tf_data['low'], tf_data['close'], self.atr_length)
                    current_atr = atr.iloc[-1]
                    tp_dist = current_atr * self.atr_multiplier_tp
            except Exception as e:
                self.logger.error(f"Error calculating dynamic TP: {e}")
        
        if side == 'long':
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
        Exit logic beyond stops: Time-based stop (Time Decay).
        If a volatility breakout doesn't materialize into a trend quickly,
        the premise of the trade is broken. Exit after 16 hours if not in profit.
        """
        if hasattr(position, 'entry_time') and position.entry_time:
            entry_time = position.entry_time
            try:
                if isinstance(entry_time, str):
                    entry_time = pd.to_datetime(entry_time)
                
                from datetime import datetime, timedelta
                
                # CRITICAL: Use simulation time from current_data if available, else fallback to real time
                now = current_data.get('timestamp') if current_data and 'timestamp' in current_data else datetime.now()
                if isinstance(now, str):
                    now = pd.to_datetime(now)
                
                # Timezone awareness safety
                if entry_time.tzinfo and not getattr(now, 'tzinfo', None):
                   now = now.astimezone() if hasattr(now, 'astimezone') else now.replace(tzinfo=entry_time.tzinfo)
                elif not getattr(entry_time, 'tzinfo', None) and getattr(now, 'tzinfo', None):
                   entry_time = entry_time.replace(tzinfo=now.tzinfo)

                time_held = now - entry_time
                
                # Check Time Decay (e.g. breakout failed to trend)
                if time_held > timedelta(hours=self.time_decay_hours):
                    side = getattr(position, 'side', None)
                    entry_price = getattr(position, 'entry_price', current_price)
                    
                    pnl_pct = 0.0
                    if side == 'long':
                         pnl_pct = (current_price - entry_price) / entry_price
                    elif side == 'short':
                         pnl_pct = (entry_price - current_price) / entry_price
                         
                    # Exit if stagnant (less than 1% profit after 16 hours)
                    if pnl_pct < 0.01:
                        return True, f"time_decay_stop (held {time_held}, pnl {pnl_pct*100:.2f}%)"
                        
            except Exception as e:
                self.logger.warning(f"Error checking time decay in breakout: {e}")
                
        return False, None
    def calculate_signal_strength(self, ohlcv: Dict[str, pd.DataFrame], symbol: str = None, signal_context: Dict[str, Any] = None) -> float:
        """
        Calculate signal strength based on Trend Persistence (Hurst Exponent).
        
        Mapping:
        - Hurst 0.5 -> 0.5 Strength
        - Hurst 1.0 -> 1.0 Strength
        """
        if signal_context and 'hurst' in signal_context:
            hurst = float(signal_context['hurst'])
            # Clamp and pass through as it maps naturally (0.5 to 1.0)
            return max(0.5, min(1.0, hurst))
            
        return 0.5
