import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch
from src.strategies.cross_sectional_momentum_strategy import CrossSectionalMomentumStrategy

class TestCSMOptimization:
    @pytest.fixture
    def config(self):
        return {
            'strategies': {
                'ohlcv_limit': 100,
                'cross_sectional_momentum': {
                    'lookback_period': 24,
                    'top_n_percent': 0.1,
                    'adx_threshold': 25
                }
            }
        }

    @pytest.fixture
    def strategy(self, config):
        return CrossSectionalMomentumStrategy(config, timeframe='1h')

    def test_adx_regime_filter(self, strategy):
        """Test that CSM signals are blocked in low ADX regimes."""
        dates = pd.date_range(start='2024-01-01', periods=49, freq='1h')
        closes = np.linspace(100, 110, 49)
        
        df = pd.DataFrame({
            'open': closes, 'high': closes+1, 'low': closes-1, 'close': closes, 'volume': 1000
        }, index=dates)
        
        # Populate universe so rank is valid
        strategy._universe_stats = {
            'BTC': {'return': 0.10, 'timestamp': pd.Timestamp.now()},
            'ETH': {'return': 0.01, 'timestamp': pd.Timestamp.now()},
            'SOL': {'return': 0.001, 'timestamp': pd.Timestamp.now()},
            'ADA': {'return': 0.001, 'timestamp': pd.Timestamp.now()},
            'DOT': {'return': 0.001, 'timestamp': pd.Timestamp.now()},
        }
        # BTC is top winner -> Should Buy if ADX OK
        
        # Case A: Low ADX (< 25) -> Block
        with patch('src.strategies.cross_sectional_momentum_strategy.calculate_adx') as mock_adx:
            mock_adx.return_value = pd.Series([15.0] * 49) # Weak trend
            
            # Need to mock ohlcv fetch because generate_signal calls it
            # But here we assume we test internal logic or pass dict
            # _generate_signal_internal signature: (ohlcv, symbol, full_ohlcv=None)
            
            sig = strategy._generate_signal_internal(df, "BTC")
            assert sig is None

        # Case B: High ADX (> 25) -> Allow
        with patch('src.strategies.cross_sectional_momentum_strategy.calculate_adx') as mock_adx:
            mock_adx.return_value = pd.Series([40.0] * 49) # Strong trend
            
            sig = strategy._generate_signal_internal(df, "BTC")
            assert sig is not None
            assert sig['signal'] == 'buy'
