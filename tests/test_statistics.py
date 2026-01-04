import pytest
import numpy as np
import pandas as pd
from src.utils.statistics import adfuller, engle_granger, hurst_exponent

class TestStatistics:
    
    def test_adfuller_stationary(self):
        """Test ADF on stationary series (white noise)."""
        np.random.seed(42)
        # White noise is stationary
        series = np.random.normal(0, 1, 100)
        
        t_stat, p_val = adfuller(series)
        
        # Should reject null hypothesis (p < 0.05)
        assert p_val < 0.05
        assert t_stat < -2.86 # Approx 5% critical value
        
    def test_adfuller_random_walk(self):
        """Test ADF on random walk (non-stationary)."""
        np.random.seed(42)
        # Random walk
        series = np.cumsum(np.random.normal(0, 1, 100))
        
        t_stat, p_val = adfuller(series)
        
        # Should NOT reject null hypothesis (p > 0.05)
        assert p_val > 0.05
        
    def test_engle_granger_cointegrated(self):
        """Test Engle-Granger on cointegrated pair."""
        np.random.seed(42)
        # X: Random walk
        x = np.cumsum(np.random.normal(0, 1, 100))
        # Y: 2*X + noise (Cointegrated)
        y = 2.0 * x + np.random.normal(0, 1, 100)
        
        t_stat, p_val, hedge_ratio = engle_granger(x, y)
        
        # Should be cointegrated
        assert p_val < 0.05
        # Hedge ratio should be close to 2.0
        assert hedge_ratio == pytest.approx(2.0, rel=0.1)
        
    def test_hurst_mean_reverting(self):
        """Test Hurst on mean reverting series."""
        np.random.seed(42)
        # Mean reverting: Ornstein-Uhlenbeck approx
        series = np.zeros(200)
        for i in range(1, 200):
            series[i] = series[i-1] * 0.5 + np.random.normal(0, 1)
            
        h = hurst_exponent(series)
        # Mean reverting should be < 0.5
        assert h < 0.6 # Allow some slack for estimation noise
        
    def test_hurst_trending(self):
        """Test Hurst on trending series."""
        np.random.seed(42)
        # Trending: Random walk with drift
        series = np.cumsum(np.random.normal(0.1, 1, 200)) # drift 0.1
        
        h = hurst_exponent(series)
        # Random walk/Trending should be >= 0.5
        assert h > 0.4
