"""
Unit tests for user_state caching and portfolio manager fallback.
"""

import pytest
from unittest.mock import MagicMock, patch
import time


class TestUserStateCaching:
    """Test that user_state API calls are cached to prevent rate limiting."""
    
    @pytest.fixture
    def mock_config(self):
        """Minimal config for API initialization."""
        return {
            'api': {
                'base_url': 'https://api.hyperliquid.xyz',
                'private_key': '0x' + '1' * 64,
                'wallet_address': '0x' + '2' * 40,
            },
            'trading': {
                'use_portfolio_based_sizing': True,
                'max_position_size_percentage': 10.0,
                'max_positions_percentage': 50.0,
            },
            'risk_management': {
                'margin_buffer_percentage': 10.0,
                'liquidation_risk_threshold': 0.15,
            },
        }
    
    def test_user_state_caching_prevents_redundant_calls(self, mock_config):
        """Ensure user_state is cached to prevent redundant API calls."""
        from src.api.hyperliquid_api import HyperliquidAPI
        
        with patch.object(HyperliquidAPI, '_init_sdk_clients'):
            api = HyperliquidAPI(mock_config)
            api.info = MagicMock()
            api.info.user_state = MagicMock(return_value={'assetPositions': []})
            
            # First call - should hit API
            result1 = api._get_cached_user_state()
            assert api.info.user_state.call_count == 1
            
            # Second call within TTL - should return cached, NOT hit API
            result2 = api._get_cached_user_state()
            assert api.info.user_state.call_count == 1  # Still 1, no new call
            
            assert result1 == result2
    
    def test_user_state_cache_expires_after_ttl(self, mock_config):
        """Ensure cache expires after TTL and fetches fresh data."""
        from src.api.hyperliquid_api import HyperliquidAPI
        
        with patch.object(HyperliquidAPI, '_init_sdk_clients'):
            api = HyperliquidAPI(mock_config)
            api.info = MagicMock()
            api.info.user_state = MagicMock(return_value={'assetPositions': []})
            
            # Set short TTL BEFORE first call
            api._user_state_cache_ttl = 0.1  # 100ms TTL for testing
            
            # First call - populates cache
            api._get_cached_user_state()
            assert api.info.user_state.call_count == 1
            
            # Force cache to expire by backdating cache_time
            api._user_state_cache_time = time.time() - 1.0  # 1 second ago
            
            # Second call after TTL - should hit API again
            api._get_cached_user_state()
            assert api.info.user_state.call_count == 2
    
    def test_user_state_cache_fallback_on_api_failure(self, mock_config):
        """Ensure stale cache is used if API call fails."""
        from src.api.hyperliquid_api import HyperliquidAPI
        
        with patch.object(HyperliquidAPI, '_init_sdk_clients'):
            api = HyperliquidAPI(mock_config)
            api.info = MagicMock()
            
            # First call succeeds
            api.info.user_state = MagicMock(return_value={'assetPositions': [{'test': 'data'}]})
            result1 = api._get_cached_user_state()
            assert result1 == {'assetPositions': [{'test': 'data'}]}
            
            # Force cache to expire
            api._user_state_cache_time = 0
            
            # Second call fails
            api.info.user_state = MagicMock(side_effect=Exception("429 Rate Limit"))
            result2 = api._get_cached_user_state()
            
            # Should return stale cache instead of None
            assert result2 == {'assetPositions': [{'test': 'data'}]}


class TestPortfolioManagerFallback:
    """Test portfolio manager uses cached equity when API fails."""
    
    @pytest.fixture
    def mock_config(self):
        return {
            'trading': {
                'use_portfolio_based_sizing': True,
                'max_position_size_percentage': 10.0,
                'max_positions_percentage': 50.0,
            },
        }
    
    def test_available_capital_fallback_on_zero_equity(self, mock_config):
        """Ensure portfolio manager uses cached equity when current is 0."""
        from src.utils.portfolio_manager import PortfolioManager
        
        pm = PortfolioManager(mock_config)
        
        # Simulate successful API update
        pm.total_equity = 10000.0
        pm.free_margin = 5000.0
        pm._last_known_equity = 10000.0
        pm._last_known_free_margin = 5000.0
        
        # Verify normal operation
        available1 = pm.calculate_available_capital_for_trading()
        assert available1 > 0
        
        # Simulate API failure (equity becomes 0)
        pm.total_equity = 0.0
        pm.free_margin = 0.0
        
        # Should use fallback cached values
        available2 = pm.calculate_available_capital_for_trading()
        assert available2 > 0, "Should use cached equity when API fails"
        assert available2 == available1, "Fallback should return same value as cached"
    
    def test_update_portfolio_caches_good_values(self, mock_config):
        """Ensure update_portfolio_info caches equity when successful."""
        from src.utils.portfolio_manager import PortfolioManager
        
        pm = PortfolioManager(mock_config)
        
        mock_api = MagicMock()
        mock_api.get_account_balance.return_value = {
            'total_equity': 15000.0,
            'free_margin': 8000.0,
            'used_margin': 7000.0,
            'unrealized_pnl': 500.0,
        }
        
        pm.update_portfolio_info(mock_api)
        
        # Verify values were cached
        assert pm._last_known_equity == 15000.0
        assert pm._last_known_free_margin == 8000.0
