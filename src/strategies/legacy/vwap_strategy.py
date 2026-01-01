"""
VWAP Mean Reversion Strategy.
"""

import logging
from typing import Dict, Any, Optional
import pandas as pd
import numpy as np
from ..base_strategy import BaseStrategy


class VWAPStrategy(BaseStrategy):
    """
    VWAP Mean Reversion Strategy.
    
    This strategy uses Volume Weighted Average Price (VWAP) and standard deviation bands
    to identify overextended price levels for mean reversion trades.
    """
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        
        # Strategy parameters
        self.std_dev_mult = config['strategies'].get('vwap', {}).get('std_dev_mult', 2.0)
        self.rsi_period = config['strategies'].get('vwap', {}).get('rsi_period', 14)
        self.rsi_overbought = config['strategies'].get('vwap', {}).get('rsi_overbought', 70)
        self.rsi_oversold = config['strategies'].get('vwap', {}).get('rsi_oversold', 30)
        
        self.logger.info(f"Initialized VWAP Strategy: std_dev={self.std_dev_mult}, rsi_period={self.rsi_period}")
    
    def calculate_vwap(self, ohlcv: pd.DataFrame) -> pd.DataFrame:
        """
        Calculate VWAP and bands.
        Note: This is a simplified rolling VWAP for timeframe continuity, 
        as true VWAP resets daily which requires session awareness.
        
        Args:
            ohlcv: OHLCV data DataFrame
            
        Returns:
            DataFrame with VWAP columns
        """
        df = ohlcv.copy()
        
        # Typical Price
        df['tp'] = (df['high'] + df['low'] + df['close']) / 3
        df['tp_vol'] = df['tp'] * df['volume']
        
        # Rolling VWAP (using a window to approximate session or recent history)
        # A 24-hour window on 1m candles is 1440 periods
        window = 1440 if self.timeframe == '1m' else 288 # Approx 1 day
        
        df['cum_vol'] = df['volume'].rolling(window=window).sum()
        df['cum_tp_vol'] = df['tp_vol'].rolling(window=window).sum()
        df['vwap'] = df['cum_tp_vol'] / df['cum_vol']
        
        # Calculate Standard Deviation Bands
        # We use the standard deviation of the price relative to VWAP
        df['variance'] = ((df['tp'] - df['vwap']) ** 2) * df['volume']
        df['cum_variance'] = df['variance'].rolling(window=window).sum()
        df['std_dev'] = np.sqrt(df['cum_variance'] / df['cum_vol'])
        
        df['upper_band'] = df['vwap'] + (df['std_dev'] * self.std_dev_mult)
        df['lower_band'] = df['vwap'] - (df['std_dev'] * self.std_dev_mult)
        
        return df
        
    def calculate_rsi(self, prices: pd.Series, period: int = 14) -> float:
        """Calculate RSI."""
        if len(prices) < period + 1:
            return 50.0
            
        delta = prices.diff()
        gains = delta.where(delta > 0, 0)
        losses = -delta.where(delta < 0, 0)
        
        avg_gains = gains.rolling(window=period).mean()
        avg_losses = losses.rolling(window=period).mean()
        
        if avg_losses.iloc[-1] == 0:
            return 100.0 if avg_gains.iloc[-1] > 0 else 50.0
            
        rs = avg_gains / avg_losses
        rsi = 100 - (100 / (1 + rs))
        return rsi.iloc[-1]
    
    def generate_signal(self, ohlcv: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        Generate trading signal based on OHLCV data.
        
        Args:
            ohlcv: OHLCV data DataFrame
            
        Returns:
            Signal dictionary or None if no signal
        """
        if ohlcv is None or len(ohlcv) < 100: # Need enough data for rolling VWAP
            return None
        
        # Calculate VWAP and Bands
        vwap_df = self.calculate_vwap(ohlcv)
        
        current_price = vwap_df['close'].iloc[-1]
        upper_band = vwap_df['upper_band'].iloc[-1]
        lower_band = vwap_df['lower_band'].iloc[-1]
        vwap = vwap_df['vwap'].iloc[-1]
        
        # Calculate RSI for confirmation
        rsi = self.calculate_rsi(ohlcv['close'], self.rsi_period)
        
        # Determine signal
        signal = 'hold'
        reason = ''
        
        # Mean Reversion Long: Price touches Lower Band AND RSI Oversold
        if current_price <= lower_band and rsi < self.rsi_oversold:
            signal = 'buy'
            reason = f'VWAP Mean Reversion (Long): Price {current_price:.2f} <= Lower Band {lower_band:.2f} & RSI {rsi:.1f}'
            
        # Mean Reversion Short: Price touches Upper Band AND RSI Overbought
        elif current_price >= upper_band and rsi > self.rsi_overbought:
            signal = 'sell'
            reason = f'VWAP Mean Reversion (Short): Price {current_price:.2f} >= Upper Band {upper_band:.2f} & RSI {rsi:.1f}'
            
        if signal == 'hold':
            return None
            
        return {
            'signal': signal,
            'reason': reason,
            'price': current_price,
            'strategy': 'vwap',
            'vwap': vwap,
            'target_price': vwap # Target is return to VWAP
        }

