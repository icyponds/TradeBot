import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock
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
                    'rsi_long_threshold': 30,
                    'rsi_short_threshold': 70
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

        # Case B: RSI = 25 (Low) -> Should Allow (if Trend is favorable)
        with patch('src.strategies.adaptive_grid_strategy.calculate_rsi') as mock_rsi:
            mock_rsi.return_value = pd.Series([25] * 100)
            with patch('src.strategies.adaptive_grid_strategy.calculate_adx') as mock_adx:
                mock_adx.return_value = pd.Series([20] * 100)
                # Mock EMA to be flat 100 (Neutral Slope) so drop to 90 is bought
                with patch('pandas.Series.ewm') as mock_ewm:
                    mock_mean = MagicMock()
                    # Return a Series of 100s
                    mock_mean.mean.return_value = pd.Series([100.0] * 100)
                    mock_ewm.return_value = mock_mean
            
                    signal = strategy._generate_signal_internal(df, "BTC")
                    assert signal is not None
                    assert signal['signal'] == 'buy'
                    assert "RSI=25.0" in signal['reason']

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

        # Case B: RSI = 75 (High) -> Should Allow (if Trend is favorable)
        with patch('src.strategies.adaptive_grid_strategy.calculate_rsi') as mock_rsi:
            mock_rsi.return_value = pd.Series([75] * 100)
            with patch('src.strategies.adaptive_grid_strategy.calculate_adx') as mock_adx:
                mock_adx.return_value = pd.Series([20] * 100)
                # Mock EMA to be flat 100 (Neutral Slope) so spike to 110 is sold
                with patch('pandas.Series.ewm') as mock_ewm:
                    mock_mean = MagicMock()
                    mock_mean.mean.return_value = pd.Series([100.0] * 100)
                    mock_ewm.return_value = mock_mean
                
                    signal = strategy._generate_signal_internal(df, "BTC")
                    assert signal is not None
                    assert signal['signal'] == 'sell'
                    assert "RSI=75.0" in signal['reason']
