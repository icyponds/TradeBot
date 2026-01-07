import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch
from src.strategies.adaptive_grid_strategy import AdaptiveGridStrategy

class TestAdaptiveGridOptimization:
    @pytest.fixture
    def config(self):
        return {
            'strategies': {
                'ohlcv_limit': 100,
                'adaptive_grid': {
                    'ema_period': 50,
                    'atr_period': 14,
                    'grid_spacing_atr': 1.5,
                    'adx_threshold': 30,
                    'rsi_period': 14,
                    'rsi_long_threshold': 40,
                    'rsi_short_threshold': 60
                }
            }
        }

    @pytest.fixture
    def strategy(self, config):
        return AdaptiveGridStrategy(config, timeframe='15m')

    def test_rsi_long_filter(self, strategy):
        """Test that Long signal is generated ONLY when RSI < threshold."""
        # Create synthetic data
        dates = pd.date_range(start='2024-01-01', periods=100, freq='15min')
        closes = np.linspace(100, 100, 100) # Flat
        closes[-1] = 90 # Drop to trigger lower band
        
        df = pd.DataFrame({
            'open': closes, 'high': closes + 2, 'low': closes - 2, 'close': closes, 'volume': 1000
        }, index=dates)

        # Case A: RSI = 50 (High) -> Should Block
        with patch('src.strategies.adaptive_grid_strategy.calculate_rsi') as mock_rsi:
            mock_rsi.return_value = pd.Series([50] * 100)
            with patch('src.strategies.adaptive_grid_strategy.calculate_adx') as mock_adx:
                mock_adx.return_value = pd.Series([20] * 100) # Low ADX
                
                signal = strategy._generate_signal_internal(df, "BTC")
                assert signal is None

        # Case B: RSI = 30 (Low) -> Should Allow
        with patch('src.strategies.adaptive_grid_strategy.calculate_rsi') as mock_rsi:
            mock_rsi.return_value = pd.Series([30] * 100)
            with patch('src.strategies.adaptive_grid_strategy.calculate_adx') as mock_adx:
                mock_adx.return_value = pd.Series([20] * 100)
                
                signal = strategy._generate_signal_internal(df, "BTC")
                assert signal is not None
                assert signal['signal'] == 'buy'
                assert "RSI=30.0" in signal['reason']

    def test_rsi_short_filter(self, strategy):
        """Test that Short signal is generated ONLY when RSI > threshold."""
        dates = pd.date_range(start='2024-01-01', periods=100, freq='15min')
        closes = np.linspace(100, 100, 100)
        closes[-1] = 110 # Spike
        
        df = pd.DataFrame({
            'open': closes, 'high': closes + 2, 'low': closes - 2, 'close': closes, 'volume': 1000
        }, index=dates)

        # Case A: RSI = 50 (Low) -> Should Block
        with patch('src.strategies.adaptive_grid_strategy.calculate_rsi') as mock_rsi:
            mock_rsi.return_value = pd.Series([50] * 100)
            with patch('src.strategies.adaptive_grid_strategy.calculate_adx') as mock_adx:
                mock_adx.return_value = pd.Series([20] * 100)
                
                signal = strategy._generate_signal_internal(df, "BTC")
                assert signal is None

        # Case B: RSI = 70 (High) -> Should Allow
        with patch('src.strategies.adaptive_grid_strategy.calculate_rsi') as mock_rsi:
            mock_rsi.return_value = pd.Series([70] * 100)
            with patch('src.strategies.adaptive_grid_strategy.calculate_adx') as mock_adx:
                mock_adx.return_value = pd.Series([20] * 100)
                
                signal = strategy._generate_signal_internal(df, "BTC")
                assert signal is not None
                assert signal['signal'] == 'sell'
                assert "RSI=70.0" in signal['reason']
