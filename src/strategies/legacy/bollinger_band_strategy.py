"""
Bollinger Band Squeeze Strategy.
"""

import logging
from typing import Dict, Any, Optional
import pandas as pd
import numpy as np
from ..base_strategy import BaseStrategy


class BollingerBandSqueezeStrategy(BaseStrategy):
    """
    Bollinger Band Squeeze Strategy.
    
    This strategy identifies periods of low volatility (squeeze) followed by a breakout.
    It uses Bollinger Bands and Keltner Channels to identify the squeeze and breakout.
    """
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        
        # Strategy parameters
        self.bb_period = config['strategies'].get('bollinger_band', {}).get('period', 20)
        self.bb_std = config['strategies'].get('bollinger_band', {}).get('std_dev', 2.0)
        self.kc_mult = config['strategies'].get('bollinger_band', {}).get('kc_mult', 1.5)
        
        self.logger.info(f"Initialized Bollinger Band Squeeze Strategy: period={self.bb_period}, std={self.bb_std}, kc_mult={self.kc_mult}")
    
    def generate_signal(self, ohlcv: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        Generate trading signal based on OHLCV data.
        
        Args:
            ohlcv: OHLCV data DataFrame
            
        Returns:
            Signal dictionary or None if no signal
        """
        if ohlcv is None or len(ohlcv) < self.bb_period + 1:
            return None
        
        # Calculate Bollinger Bands
        sma = ohlcv['close'].rolling(window=self.bb_period).mean()
        std = ohlcv['close'].rolling(window=self.bb_period).std()
        
        upper_bb = sma + (std * self.bb_std)
        lower_bb = sma - (std * self.bb_std)
        
        # Calculate Keltner Channels
        # TR = max(high-low, abs(high-prev_close), abs(low-prev_close))
        high_low = ohlcv['high'] - ohlcv['low']
        high_close = (ohlcv['high'] - ohlcv['close'].shift()).abs()
        low_close = (ohlcv['low'] - ohlcv['close'].shift()).abs()
        
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = tr.rolling(window=self.bb_period).mean()
        
        upper_kc = sma + (atr * self.kc_mult)
        lower_kc = sma - (atr * self.kc_mult)
        
        # Current values
        current_price = ohlcv['close'].iloc[-1]
        prev_price = ohlcv['close'].iloc[-2]
        
        curr_upper_bb = upper_bb.iloc[-1]
        curr_lower_bb = lower_bb.iloc[-1]
        curr_upper_kc = upper_kc.iloc[-1]
        curr_lower_kc = lower_kc.iloc[-1]
        
        prev_upper_bb = upper_bb.iloc[-2]
        prev_lower_bb = lower_bb.iloc[-2]
        prev_upper_kc = upper_kc.iloc[-2]
        prev_lower_kc = lower_kc.iloc[-2]
        
        # Check for Squeeze (Bollinger Bands inside Keltner Channels)
        # We look for a squeeze in the recent past (e.g., previous candle)
        was_squeeze = (prev_upper_bb < prev_upper_kc) and (prev_lower_bb > prev_lower_kc)
        
        # Determine signal
        signal = 'hold'
        reason = ''
        
        # Breakout signals
        # Buy: Price closes above Upper BB after a squeeze
        if was_squeeze and current_price > curr_upper_bb:
            signal = 'buy'
            reason = f'Bollinger Band Squeeze Breakout (Long): Price {current_price:.2f} > Upper BB {curr_upper_bb:.2f}'
            
        # Sell: Price closes below Lower BB after a squeeze
        elif was_squeeze and current_price < curr_lower_bb:
            signal = 'sell'
            reason = f'Bollinger Band Squeeze Breakout (Short): Price {current_price:.2f} < Lower BB {curr_lower_bb:.2f}'
            
        if signal == 'hold':
            return None
            
        return {
            'signal': signal,
            'reason': reason,
            'price': current_price,
            'strategy': 'bollinger_band_squeeze',
            'upper_bb': curr_upper_bb,
            'lower_bb': curr_lower_bb
        }

    def calculate_signal_strength(self, ohlcv: pd.DataFrame) -> float:
        """
        Calculate signal strength based on Bandwidth Squeeze.
        
        Args:
            ohlcv: OHLCV data DataFrame
            
        Returns:
            Signal strength (0-1)
        """
        if ohlcv is None or len(ohlcv) < self.bb_period:
            return 0.5
            
        # Calculate Bollinger Bands
        sma = ohlcv['close'].rolling(window=self.bb_period).mean()
        std = ohlcv['close'].rolling(window=self.bb_period).std()
        
        upper_bb = sma + (std * self.bb_std)
        lower_bb = sma - (std * self.bb_std)
        
        if sma.iloc[-1] == 0:
            return 0.5
            
        bandwidth = (upper_bb - lower_bb) / sma
        
        # Lower bandwidth = tighter squeeze = stronger potential move
        # Normalize: 0.05 bandwidth -> 1.0 strength, 0.25+ bandwidth -> 0.0 strength
        # Using *5 factor from original manager logic
        strength = max(0.0, min(1.0, 1.25 - (bandwidth.iloc[-1] * 5)))
        
        return strength

