"""
Moving Average Crossover Strategy.
"""

import logging
from typing import Dict, Any, Optional
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy


class MovingAverageStrategy(BaseStrategy):
    """Moving average crossover strategy."""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        
        # Strategy parameters
        self.short_period = config['strategies']['moving_average']['short_period']
        self.long_period = config['strategies']['moving_average']['long_period']
        
        self.logger.info(f"Initialized Moving Average Strategy: {self.short_period}/{self.long_period}")
    
    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: pd.DataFrame = None, 
                            signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """
        Calculate take profit based on moving average trend strength.
        
        Args:
            entry_price: Entry price
            side: 'buy' or 'sell'
            ohlcv: OHLCV data for trend analysis
            signal_strength: Signal strength (0.0 to 1.0)
            market_volatility: Market volatility factor
            
        Returns:
            Take profit price
        """
        if ohlcv is None or len(ohlcv) < self.long_period:
            # Fallback to base implementation
            return super().calculate_take_profit(entry_price, side, ohlcv, signal_strength, market_volatility)
        
        # Calculate moving averages
        short_ma = ohlcv['close'].rolling(window=self.short_period).mean()
        long_ma = ohlcv['close'].rolling(window=self.long_period).mean()
        
        if len(short_ma) < 2 or len(long_ma) < 2:
            return super().calculate_take_profit(entry_price, side, ohlcv, signal_strength, market_volatility)
        
        current_price = ohlcv['close'].iloc[-1]
        short_ma_current = short_ma.iloc[-1]
        long_ma_current = long_ma.iloc[-1]
        
        # Calculate trend strength
        ma_spread = abs(short_ma_current - long_ma_current) / current_price
        trend_strength = min(ma_spread * 10, 1.0)  # Normalize to 0-1
        
        # Calculate volatility-based adjustment
        price_volatility = ohlcv['close'].pct_change().std()
        volatility_factor = min(price_volatility * 100, 2.0)  # Cap at 2x
        
        # Base take profit percentage
        base_percentage = 0.06  # 6% base
        
        # Adjust based on trend strength and volatility
        trend_adjustment = 1.0 + (trend_strength * 0.5)  # Up to 50% increase for strong trends
        volatility_adjustment = 1.0 + (volatility_factor * 0.3)  # Up to 30% increase for high volatility
        
        # Final take profit percentage
        take_profit_percentage = base_percentage * trend_adjustment * volatility_adjustment * signal_strength
        
        # Ensure reasonable bounds (2% to 15%)
        take_profit_percentage = max(0.02, min(0.15, take_profit_percentage))
        
        if side == 'buy':
            return entry_price * (1 + take_profit_percentage)
        else:
            return entry_price * (1 - take_profit_percentage)
    
    def generate_signal(self, ohlcv: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        Generate trading signal based on OHLCV data.
        
        Args:
            ohlcv: OHLCV data DataFrame
            
        Returns:
            Signal dictionary or None if no signal
        """
        if ohlcv is None or len(ohlcv) < self.long_period:
            return None
        
        # Calculate moving averages
        short_ma = ohlcv['close'].rolling(window=self.short_period).mean()
        long_ma = ohlcv['close'].rolling(window=self.long_period).mean()
        
        if len(short_ma) < 2 or len(long_ma) < 2:
            return None
        
        current_price = ohlcv['close'].iloc[-1]
        short_ma_current = short_ma.iloc[-1]
        long_ma_current = long_ma.iloc[-1]
        
        # Previous values for crossover detection
        short_ma_prev = short_ma.iloc[-2]
        long_ma_prev = long_ma.iloc[-2]
        
        # Determine signal
        signal = 'hold'
        reason = ''
        
        # Bullish crossover (short MA crosses above long MA)
        if (short_ma_prev <= long_ma_prev and short_ma_current > long_ma_current):
            signal = 'buy'
            reason = f'Bullish crossover: Short MA ({short_ma_current:.2f}) > Long MA ({long_ma_current:.2f})'
        
        # Bearish crossover (short MA crosses below long MA)
        elif (short_ma_prev >= long_ma_prev and short_ma_current < long_ma_current):
            signal = 'sell'
            reason = f'Bearish crossover: Short MA ({short_ma_current:.2f}) < Long MA ({long_ma_current:.2f})'
        
        # Strong trend signals
        elif short_ma_current > long_ma_current and current_price > short_ma_current:
            signal = 'buy'
            reason = f'Strong uptrend: Price ({current_price:.2f}) > Short MA ({short_ma_current:.2f}) > Long MA ({long_ma_current:.2f})'
        
        elif short_ma_current < long_ma_current and current_price < short_ma_current:
            signal = 'sell'
            reason = f'Strong downtrend: Price ({current_price:.2f}) < Short MA ({short_ma_current:.2f}) < Long MA ({long_ma_current:.2f})'
        
        if signal == 'hold':
            return None
        
        return {
            'signal': signal,
            'reason': reason,
            'price': current_price,
            'short_ma': short_ma_current,
            'long_ma': long_ma_current,
            'strategy': 'moving_average',
        }
    
    def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze market data using moving averages.
        
        Args:
            market_data: Market data dictionary
            
        Returns:
            Analysis results with signals
        """
        symbol = market_data['symbol']
        ohlcv = market_data['ohlcv']
        current_price = market_data['current_price']
        
        if ohlcv is None or len(ohlcv) < self.long_period:
            return {
                'signal': 'hold',
                'reason': 'Insufficient data',
                'short_ma': None,
                'long_ma': None,
                'current_price': current_price,
            }
        
        # Calculate moving averages
        short_ma = ohlcv['close'].rolling(window=self.short_period).mean().iloc[-1]
        long_ma = ohlcv['close'].rolling(window=self.long_period).mean().iloc[-1]
        
        # Previous values for crossover detection
        prev_short_ma = ohlcv['close'].rolling(window=self.short_period).mean().iloc[-2]
        prev_long_ma = ohlcv['close'].rolling(window=self.long_period).mean().iloc[-2]
        
        # Determine signal
        signal = 'hold'
        reason = ''
        
        # Bullish crossover (short MA crosses above long MA)
        if (prev_short_ma <= prev_long_ma and short_ma > long_ma):
            signal = 'buy'
            reason = f'Bullish crossover: Short MA ({short_ma:.2f}) > Long MA ({long_ma:.2f})'
        
        # Bearish crossover (short MA crosses below long MA)
        elif (prev_short_ma >= prev_long_ma and short_ma < long_ma):
            signal = 'sell'
            reason = f'Bearish crossover: Short MA ({short_ma:.2f}) < Long MA ({long_ma:.2f})'
        
        # Strong trend signals
        elif short_ma > long_ma and current_price > short_ma:
            signal = 'buy'
            reason = f'Strong uptrend: Price ({current_price:.2f}) > Short MA ({short_ma:.2f}) > Long MA ({long_ma:.2f})'
        
        elif short_ma < long_ma and current_price < short_ma:
            signal = 'sell'
            reason = f'Strong downtrend: Price ({current_price:.2f}) < Short MA ({short_ma:.2f}) < Long MA ({long_ma:.2f})'
        
        return {
            'signal': signal,
            'reason': reason,
            'short_ma': short_ma,
            'long_ma': long_ma,
            'current_price': current_price,
            'short_period': self.short_period,
            'long_period': self.long_period,
        }
    
    def should_buy(self, analysis: Dict[str, Any]) -> bool:
        """
        Determine if we should buy based on analysis.
        
        Args:
            analysis: Analysis results from analyze() method
            
        Returns:
            True if should buy, False otherwise
        """
        return analysis['signal'] == 'buy'
    
    def should_sell(self, analysis: Dict[str, Any]) -> bool:
        """
        Determine if we should sell based on analysis.
        
        Args:
            analysis: Analysis results from analyze() method
            
        Returns:
            True if should sell, False otherwise
        """
        return analysis['signal'] == 'sell' 