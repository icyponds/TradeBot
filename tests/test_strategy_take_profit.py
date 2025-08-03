"""
Test strategy-specific take profit calculations.
"""

import unittest
import pandas as pd
import numpy as np
from unittest.mock import Mock

from src.strategies.moving_average_strategy import MovingAverageStrategy
from src.strategies.rsi_strategy import RSIStrategy


class TestStrategyTakeProfit(unittest.TestCase):
    """Test strategy-specific take profit calculations."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Mock configuration
        self.config = {
            'trading': {
                'max_position_size': 50,
                'risk_percentage': 2.0,
                'stop_loss_percentage': 2.0,
            },
            'strategies': {
                'timeframe': '1m',
                'ohlcv_limit': 100,
                'moving_average': {
                    'short_period': 5,
                    'long_period': 10,
                },
                'rsi': {
                    'period': 14,
                    'overbought': 70,
                    'oversold': 30,
                },
            },
        }
        
        # Create sample OHLCV data
        self.sample_ohlcv = pd.DataFrame({
            'open': [100] * 20,
            'high': [105] * 20,
            'low': [95] * 20,
            'close': [100 + i * 0.5 for i in range(20)],  # Upward trend
            'volume': [1000] * 20,
        })
        
        # Initialize strategies
        self.ma_strategy = MovingAverageStrategy(self.config)
        self.rsi_strategy = RSIStrategy(self.config)
    
    def test_moving_average_take_profit(self):
        """Test moving average strategy take profit calculation."""
        entry_price = 100.0
        side = 'buy'
        signal_strength = 0.8
        market_volatility = 1.2
        
        # Test with upward trending data
        take_profit = self.ma_strategy.calculate_take_profit(
            entry_price, side, self.sample_ohlcv, signal_strength, market_volatility
        )
        
        # Should be higher than entry price for buy side
        self.assertGreater(take_profit, entry_price)
        
        # Test with sell side
        take_profit_sell = self.ma_strategy.calculate_take_profit(
            entry_price, 'sell', self.sample_ohlcv, signal_strength, market_volatility
        )
        
        # Should be lower than entry price for sell side
        self.assertLess(take_profit_sell, entry_price)
    
    def test_rsi_take_profit(self):
        """Test RSI strategy take profit calculation."""
        entry_price = 100.0
        side = 'buy'
        signal_strength = 0.9
        market_volatility = 1.1
        
        # Test with normal RSI data
        take_profit = self.rsi_strategy.calculate_take_profit(
            entry_price, side, self.sample_ohlcv, signal_strength, market_volatility
        )
        
        # Should be higher than entry price for buy side
        self.assertGreater(take_profit, entry_price)
        
        # Test with sell side
        take_profit_sell = self.rsi_strategy.calculate_take_profit(
            entry_price, 'sell', self.sample_ohlcv, signal_strength, market_volatility
        )
        
        # Should be lower than entry price for sell side
        self.assertLess(take_profit_sell, entry_price)
    
    def test_fallback_to_base_implementation(self):
        """Test fallback to base implementation when insufficient data."""
        entry_price = 100.0
        side = 'buy'
        
        # Test with insufficient data
        insufficient_ohlcv = pd.DataFrame({
            'open': [100] * 5,
            'high': [105] * 5,
            'low': [95] * 5,
            'close': [100] * 5,
            'volume': [1000] * 5,
        })
        
        # Both strategies should fallback to base implementation
        ma_take_profit = self.ma_strategy.calculate_take_profit(
            entry_price, side, insufficient_ohlcv
        )
        
        rsi_take_profit = self.rsi_strategy.calculate_take_profit(
            entry_price, side, insufficient_ohlcv
        )
        
        # Both should return valid take profit prices
        self.assertGreater(ma_take_profit, entry_price)
        self.assertGreater(rsi_take_profit, entry_price)
    
    def test_take_profit_bounds(self):
        """Test that take profit percentages stay within reasonable bounds."""
        entry_price = 100.0
        side = 'buy'
        
        # Test with extreme values
        extreme_signal_strength = 2.0
        extreme_volatility = 3.0
        
        ma_take_profit = self.ma_strategy.calculate_take_profit(
            entry_price, side, self.sample_ohlcv, extreme_signal_strength, extreme_volatility
        )
        
        rsi_take_profit = self.rsi_strategy.calculate_take_profit(
            entry_price, side, self.sample_ohlcv, extreme_signal_strength, extreme_volatility
        )
        
        # Calculate percentages
        ma_percentage = abs(ma_take_profit - entry_price) / entry_price
        rsi_percentage = abs(rsi_take_profit - entry_price) / entry_price
        
        # Should be within reasonable bounds (2% to 20%)
        self.assertGreaterEqual(ma_percentage, 0.02)
        self.assertLessEqual(ma_percentage, 0.20)
        
        self.assertGreaterEqual(rsi_percentage, 0.02)
        self.assertLessEqual(rsi_percentage, 0.20)


if __name__ == '__main__':
    unittest.main() 