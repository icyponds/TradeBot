import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock
from src.strategies.sentiment_ml_strategy import SentimentMLStrategy

class TestSentimentMLOptimization:
    @pytest.fixture
    def config(self):
        return {
            'strategies': {
                'ohlcv_limit': 100,
                'sentiment_ml': {
                    'sentiment_threshold': 2.0,
                    'normalization_lookback': 24,
                    'trend_ema_period': 50
                }
            }
        }

    @pytest.fixture
    def strategy(self, config):
        return SentimentMLStrategy(config, timeframe='1h')

    def test_trend_filter(self, strategy):
        """Test that sentiment signals are filtered by EMA trend."""
        # Create data
        dates = pd.date_range(start='2024-01-01', periods=100, freq='1h')
        # Price is 100. EMA(50) ~ 100.
        closes = np.array([100.0] * 100)
        
        # Scenario 1: Uptrend (Price > EMA)
        # Set last price to 110 (above EMA ~100)
        closes[-1] = 110
        df_uptrend = pd.DataFrame({
            'open': closes, 'high': closes, 'low': closes, 'close': closes, 'volume': 1000
        }, index=dates)
        
        # Scenario 2: Downtrend (Price < EMA)
        closes_down = np.array([100.0] * 100)
        closes_down[-1] = 90
        df_downtrend = pd.DataFrame({
            'open': closes_down, 'high': closes_down, 'low': closes_down, 'close': closes_down, 'volume': 1000
        }, index=dates)

        # Mock sentiment to be HYPE (Z > 2)
        with patch.object(strategy, '_get_sentiment_proxy') as mock_sent:
            # We need standard deviation > 0
            sentiment_vals = np.zeros(100)
            sentiment_vals[-20:] = range(20) # ensure std > 0
            sentiment_vals[-1] = 1000 # Massive spike -> Z > 2
            mock_sent.return_value = pd.Series(sentiment_vals)
            
            # A. Hype (Long) in Uptrend -> ALLOW
            sig = strategy._generate_signal_internal(df_uptrend, "BTC")
            assert sig is not None
            assert sig['signal'] == 'buy'
            
            # B. Hype (Long) in Downtrend -> BLOCK
            sig = strategy._generate_signal_internal(df_downtrend, "BTC")
            assert sig is None

        # Mock sentiment to be FUD (Z < -2)
        with patch.object(strategy, '_get_sentiment_proxy') as mock_sent:
            sentiment_vals = np.zeros(100)
            sentiment_vals[-20:] = range(20)
            sentiment_vals[-1] = -1000 # Massive drop -> Z < -2
            mock_sent.return_value = pd.Series(sentiment_vals)
            
            # C. FUD (Short) in Downtrend -> ALLOW
            sig = strategy._generate_signal_internal(df_downtrend, "BTC")
            assert sig is not None
            assert sig['signal'] == 'sell'
            
            # D. FUD (Short) in Uptrend -> BLOCK
            sig = strategy._generate_signal_internal(df_uptrend, "BTC")
            assert sig is None
