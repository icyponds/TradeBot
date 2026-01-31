"""
Unit tests for VolatilityScaler - per-asset ATR ratio calculation for z-score scaling.
"""

import pytest
from unittest.mock import MagicMock
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from src.utils.volatility_scaler import VolatilityScaler


class TestVolatilityScalerBasic:
    """Test basic VolatilityScaler functionality."""
    
    def test_init_default_params(self):
        """Test initialization with default parameters."""
        scaler = VolatilityScaler()
        
        assert scaler.lookback == 14
        assert scaler.min_multiplier == 0.8
        assert scaler.max_multiplier == 1.5
    
    def test_init_custom_params(self):
        """Test initialization with custom parameters."""
        scaler = VolatilityScaler(
            lookback=20,
            min_multiplier=0.5,
            max_multiplier=2.0,
        )
        
        assert scaler.lookback == 20
        assert scaler.min_multiplier == 0.5
        assert scaler.max_multiplier == 2.0
    
    def test_default_ratio_is_one(self):
        """Test that default ratio is 1.0 for unknown symbols."""
        scaler = VolatilityScaler()
        
        assert scaler.get_ratio('UNKNOWN') == 1.0


class TestATRCalculation:
    """Test ATR calculation functionality."""
    
    def _create_ohlcv(self, high_range, low_range, close_start, n_bars=20):
        """Helper to create OHLCV dataframe."""
        dates = pd.date_range(end=datetime.now(), periods=n_bars, freq='1h')
        closes = [close_start + np.random.uniform(-0.5, 0.5) for _ in range(n_bars)]
        
        data = {
            'timestamp': dates,
            'open': [c - np.random.uniform(0.1, 0.5) for c in closes],
            'high': [c + np.random.uniform(high_range[0], high_range[1]) for c in closes],
            'low': [c - np.random.uniform(low_range[0], low_range[1]) for c in closes],
            'close': closes,
            'volume': [1000] * n_bars,
        }
        return pd.DataFrame(data)
    
    def test_calculate_atr_basic(self):
        """Test basic ATR calculation."""
        scaler = VolatilityScaler(lookback=14)
        
        df = self._create_ohlcv((1, 2), (1, 2), 100, n_bars=20)
        atr = scaler._calculate_atr(df)
        
        assert atr > 0
    
    def test_calculate_atr_insufficient_data(self):
        """Test ATR returns 0 with insufficient data."""
        scaler = VolatilityScaler(lookback=14)
        
        df = self._create_ohlcv((1, 2), (1, 2), 100, n_bars=5)  # Only 5 bars
        atr = scaler._calculate_atr(df)
        
        assert atr == 0.0
    
    def test_calculate_atr_none_input(self):
        """Test ATR returns 0 with None input."""
        scaler = VolatilityScaler()
        
        atr = scaler._calculate_atr(None)
        
        assert atr == 0.0


class TestVolatilityRatioCalculation:
    """Test volatility ratio calculation with multiple symbols."""
    
    def _create_ohlcv(self, volatility, n_bars=20):
        """Helper to create OHLCV with specific volatility level."""
        dates = pd.date_range(end=datetime.now(), periods=n_bars, freq='1h')
        base_price = 100
        closes = []
        current = base_price
        for _ in range(n_bars):
            current += np.random.uniform(-volatility, volatility)
            closes.append(current)
        
        data = {
            'timestamp': dates,
            'open': [c - volatility * 0.3 for c in closes],
            'high': [c + volatility * 0.5 for c in closes],
            'low': [c - volatility * 0.5 for c in closes],
            'close': closes,
            'volume': [1000] * n_bars,
        }
        return pd.DataFrame(data)
    
    def test_update_calculates_ratios(self):
        """Test that update calculates ratios for all symbols."""
        scaler = VolatilityScaler(lookback=10)
        
        # Create mock OHLCV getter
        ohlcv_data = {
            'BTC': self._create_ohlcv(volatility=2.0, n_bars=20),
            'ETH': self._create_ohlcv(volatility=3.0, n_bars=20),
            'SOL': self._create_ohlcv(volatility=5.0, n_bars=20),  # Highest vol
        }
        
        def mock_getter(symbol, tf, limit):
            return ohlcv_data.get(symbol)
        
        scaler.update(['BTC', 'ETH', 'SOL'], mock_getter, '15m')
        
        # All should have ratios
        assert scaler.get_ratio('BTC') >= 0.8
        assert scaler.get_ratio('ETH') >= 0.8
        assert scaler.get_ratio('SOL') >= 0.8
        
        # SOL should have higher ratio (more volatile)
        assert scaler.get_ratio('SOL') >= scaler.get_ratio('BTC')
    
    def test_ratio_clamping(self):
        """Test that ratios are clamped to min/max."""
        scaler = VolatilityScaler(lookback=10, min_multiplier=0.8, max_multiplier=1.5)
        
        # Create extreme volatility differences
        ohlcv_data = {
            'LOW': self._create_ohlcv(volatility=0.1, n_bars=20),  # Very low vol
            'HIGH': self._create_ohlcv(volatility=50.0, n_bars=20),  # Very high vol
        }
        
        def mock_getter(symbol, tf, limit):
            return ohlcv_data.get(symbol)
        
        scaler.update(['LOW', 'HIGH'], mock_getter, '15m')
        
        # Both should be clamped
        assert scaler.get_ratio('LOW') >= 0.8
        assert scaler.get_ratio('HIGH') <= 1.5
    
    def test_median_atr_calculation(self):
        """Test that median ATR is calculated correctly."""
        scaler = VolatilityScaler(lookback=10)
        
        ohlcv_data = {
            'A': self._create_ohlcv(volatility=1.0, n_bars=20),
            'B': self._create_ohlcv(volatility=2.0, n_bars=20),
            'C': self._create_ohlcv(volatility=3.0, n_bars=20),
        }
        
        def mock_getter(symbol, tf, limit):
            return ohlcv_data.get(symbol)
        
        scaler.update(['A', 'B', 'C'], mock_getter, '15m')
        
        median_atr = scaler.get_median_atr()
        
        assert median_atr > 0


class TestStatusMethods:
    """Test status and utility methods."""
    
    def test_get_all_ratios(self):
        """Test getting all ratios."""
        scaler = VolatilityScaler()
        
        # Initially empty
        ratios = scaler.get_all_ratios()
        assert len(ratios) == 0
    
    def test_get_atr(self):
        """Test getting raw ATR value."""
        scaler = VolatilityScaler()
        
        # Unknown symbol returns 0
        assert scaler.get_atr('UNKNOWN') == 0.0
    
    def test_get_status_summary(self):
        """Test getting status summary."""
        scaler = VolatilityScaler()
        
        summary = scaler.get_status_summary()
        
        assert 'symbol_count' in summary
        assert 'median_atr' in summary
        assert 'ratio_min' in summary
        assert 'ratio_max' in summary
        assert 'ratio_mean' in summary
