import pytest
from unittest.mock import MagicMock
import pandas as pd
import numpy as np
from src.utils.correlation_manager import CorrelationManager

class TestCorrelationManager:
    
    @pytest.fixture
    def correlation_manager(self, mock_config, mock_market_api):
        """Creates a CorrelationManager instance."""
        return CorrelationManager(mock_market_api, mock_config)

    def test_update_correlations_basic(self, correlation_manager):
        """Test basic correlation update."""
        # Mock API response with correlated data
        # Symbol A: Random walk
        # Symbol B: A + noise (High correlation)
        # Symbol C: Unrelated (Low correlation)
        
        np.random.seed(42)
        idx = pd.date_range("2024-01-01", periods=100, freq='1h')
        a = np.cumsum(np.random.randn(100))
        b = a + np.random.randn(100) * 0.1
        c = np.cumsum(np.random.randn(100)) # Different walk
        
        def get_ohlcv(symbol, tf, limit):
            if symbol == 'A': return pd.DataFrame({'close': a}, index=idx)
            if symbol == 'B': return pd.DataFrame({'close': b}, index=idx)
            if symbol == 'C': return pd.DataFrame({'close': c}, index=idx)
            return None
            
        correlation_manager.market_api.get_ohlcv.side_effect = get_ohlcv
        
        # Test
        result = correlation_manager.update_correlations(['A', 'B', 'C'])
        
        # A and B must be correlated (A->B or B->A)
        assert result.get('A') == 'B' or result.get('B') == 'A'
        
        # C should ideally not match A or B strongly, or at least A-B is strongest
        # Check that we have results
        assert len(result) > 0

    def test_cointegration_test_fallback(self, correlation_manager):
        """Test cointegration wrapper (checking mostly the structure return)."""
        s1 = pd.Series(np.random.randn(100))
        s2 = pd.Series(np.random.randn(100))
        
        # We perform a test using the internal method
        # If statsmodels is missing, it falls back. If present, it runs real test.
        # We just want to ensure it returns a valid result object.
        
        result = correlation_manager.test_cointegration(s1, s2, "A", "B")
        
        assert result.symbol_a == "A"
        assert result.symbol_b == "B"
        assert isinstance(result.p_value, float)
        assert isinstance(result.hedge_ratio, float)
