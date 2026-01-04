import pytest
from unittest.mock import MagicMock, patch
from src.utils.pair_selector import DynamicPairSelector, AssetMetrics

class TestDynamicPairSelector:

    @pytest.fixture
    def pair_selector(self, mock_config, mock_market_api):
        """Creates a DynamicPairSelector instance."""
        # Ensure config has trading section used by selector
        mock_config['trading']['dynamic_pair_selection'] = True
        mock_config['trading']['min_open_interest'] = 1000000
        mock_config['trading']['scan_interval_minutes'] = 60
        mock_config['trading']['excluded_assets'] = []
        mock_config['trading']['included_assets'] = []
        mock_config['hip3'] = {'enabled': False}
        mock_config['spot'] = {'enabled': False}
        mock_config['pair_selection'] = {
            'mode': 'sophisticated',
            'weights': {
                'liquidity': 0.25,
                'volatility': 0.20,
                'strategy_fit': 0.25,
                'diversification': 0.15,
                'historical_performance': 0.15
            }
        }
        
        return DynamicPairSelector(mock_config, mock_market_api)

    def test_initialization(self, pair_selector):
        """Test initialization."""
        assert pair_selector.selection_mode.value == 'sophisticated'
        assert pair_selector.weight_liquidity == 0.25
        assert pair_selector.weight_volatility == 0.20

    def test_liquidity_score(self, pair_selector):
        """Test liquidity score calculation."""
        # High liquidity asset
        asset_high = {
            'openInterest': 100_000_000, # 100M
            'volume24h': 100_000_000,    # 100M
            'bid': 100.0,
            'ask': 100.05               # 5bps spread
        }
        score_high = pair_selector._calculate_liquidity_score(asset_high)
        # Should be high (near 1.0)
        assert score_high > 0.8
        
        # Low liquidity asset
        asset_low = {
            'openInterest': 100_000,     # 100k
            'volume24h': 100_000,        # 100k
            'bid': 100.0,
            'ask': 101.0                 # 100bps spread
        }
        score_low = pair_selector._calculate_liquidity_score(asset_low)
        # Should be low
        assert score_low < 0.3

    def test_volatility_score_optimal(self, pair_selector):
        """Test volatility score for optimal range."""
        symbol = "BTC"
        asset = {'markPrice': 50000}
        
        # Mock price history
        import pandas as pd
        import numpy as np
        
        # Perfect volatility (e.g., 4% daily)
        # 4% std dev
        rets = np.random.normal(0, 0.04, 100)
        prices = pd.Series(100 * (1 + rets).cumprod())
        
        with patch.object(pair_selector, '_get_price_history', return_value=prices):
            score, raw_vol = pair_selector._calculate_volatility_score(symbol, asset)
            
            # Since mock is random, we check if logic runs without error and returns valid range
            assert 0.0 <= score <= 1.0
            
    def test_volatility_score_fallback(self, pair_selector):
        """Test volatility score using 24h fallback."""
        symbol = "BTC"
        asset = {
            'markPrice': 100,
            'high24h': 105,
            'low24h': 95
        }
        # Range = 10, Price = 100 -> Vol ~ 0.10 (10%)
        # Optimal max is 0.08
        
        with patch.object(pair_selector, '_get_price_history', return_value=None):
            score, raw_vol = pair_selector._calculate_volatility_score(symbol, asset)
            
            # Should be penalized for being too high (10% > 8%)
            assert score < 1.0
            assert raw_vol == pytest.approx(0.10)
