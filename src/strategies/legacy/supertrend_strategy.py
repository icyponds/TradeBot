"""
Supertrend Strategy.
"""

import logging
from typing import Dict, Any, Optional
import pandas as pd
import numpy as np
from ..base_strategy import BaseStrategy


class SupertrendStrategy(BaseStrategy):
    """
    Supertrend Strategy.
    
    This strategy uses the Supertrend indicator to identify trend direction and reversals.
    It is a trend-following strategy that works well in trending markets.
    """
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        
        # Strategy parameters
        self.atr_period = config['strategies'].get('supertrend', {}).get('atr_period', 10)
        self.multiplier = config['strategies'].get('supertrend', {}).get('multiplier', 3.0)
        
        self.logger.info(f"Initialized Supertrend Strategy: atr_period={self.atr_period}, multiplier={self.multiplier}")
    
    def calculate_supertrend(self, ohlcv: pd.DataFrame) -> pd.DataFrame:
        """
        Calculate Supertrend indicator.
        
        Args:
            ohlcv: OHLCV data DataFrame
            
        Returns:
            DataFrame with Supertrend columns
        """
        df = ohlcv.copy()
        
        # Calculate ATR
        high_low = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift()).abs()
        low_close = (df['low'] - df['close'].shift()).abs()
        
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df['atr'] = tr.rolling(window=self.atr_period).mean()
        
        # Calculate Basic Upper and Lower Bands
        hl2 = (df['high'] + df['low']) / 2
        df['basic_upper'] = hl2 + (self.multiplier * df['atr'])
        df['basic_lower'] = hl2 - (self.multiplier * df['atr'])
        
        # Initialize Final Bands and Supertrend
        df['final_upper'] = 0.0
        df['final_lower'] = 0.0
        df['supertrend'] = 0.0
        df['trend'] = 0  # 1 for Bullish, -1 for Bearish
        
        # Calculate Final Bands and Trend
        for i in range(self.atr_period, len(df)):
            # Final Upper Band
            if df['basic_upper'].iloc[i] < df['final_upper'].iloc[i-1] or df['close'].iloc[i-1] > df['final_upper'].iloc[i-1]:
                df.loc[df.index[i], 'final_upper'] = df['basic_upper'].iloc[i]
            else:
                df.loc[df.index[i], 'final_upper'] = df['final_upper'].iloc[i-1]
                
            # Final Lower Band
            if df['basic_lower'].iloc[i] > df['final_lower'].iloc[i-1] or df['close'].iloc[i-1] < df['final_lower'].iloc[i-1]:
                df.loc[df.index[i], 'final_lower'] = df['basic_lower'].iloc[i]
            else:
                df.loc[df.index[i], 'final_lower'] = df['final_lower'].iloc[i-1]
                
            # Trend
            if df['close'].iloc[i] > df['final_upper'].iloc[i-1]:
                df.loc[df.index[i], 'trend'] = 1
            elif df['close'].iloc[i] < df['final_lower'].iloc[i-1]:
                df.loc[df.index[i], 'trend'] = -1
            else:
                df.loc[df.index[i], 'trend'] = df['trend'].iloc[i-1]
                
            # Supertrend Value
            if df['trend'].iloc[i] == 1:
                df.loc[df.index[i], 'supertrend'] = df['final_lower'].iloc[i]
            else:
                df.loc[df.index[i], 'supertrend'] = df['final_upper'].iloc[i]
                
        return df
    
    def generate_signal(self, ohlcv: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        Generate trading signal based on OHLCV data.
        
        Args:
            ohlcv: OHLCV data DataFrame
            
        Returns:
            Signal dictionary or None if no signal
        """
        if ohlcv is None or len(ohlcv) < self.atr_period + 2:
            return None
        
        # Calculate Supertrend
        st_df = self.calculate_supertrend(ohlcv)
        
        current_trend = st_df['trend'].iloc[-1]
        prev_trend = st_df['trend'].iloc[-2]
        current_price = st_df['close'].iloc[-1]
        supertrend_val = st_df['supertrend'].iloc[-1]
        
        # Determine signal
        signal = 'hold'
        reason = ''
        
        # Trend Reversal: Bearish to Bullish
        if prev_trend == -1 and current_trend == 1:
            signal = 'buy'
            reason = f'Supertrend Reversal (Bullish): Price {current_price:.2f} crossed above Supertrend {supertrend_val:.2f}'
            
        # Trend Reversal: Bullish to Bearish
        elif prev_trend == 1 and current_trend == -1:
            signal = 'sell'
            reason = f'Supertrend Reversal (Bearish): Price {current_price:.2f} crossed below Supertrend {supertrend_val:.2f}'
            
        if signal == 'hold':
            return None
            
        return {
            'signal': signal,
            'reason': reason,
            'price': current_price,
            'strategy': 'supertrend',
            'supertrend_value': supertrend_val
        }

    def calculate_signal_strength(self, ohlcv: pd.DataFrame) -> float:
        """
        Calculate signal strength.
        
        Args:
            ohlcv: OHLCV data DataFrame
            
        Returns:
            Signal strength (0-1)
        """
        # For Supertrend, strength is based on trend persistence
        # Default high confidence for trend following
        return 0.8

