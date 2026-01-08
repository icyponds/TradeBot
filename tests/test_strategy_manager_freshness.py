import pytest
from unittest.mock import MagicMock, patch
from src.strategies.strategy_manager import StrategyManager

class TestStrategyManagerFreshness:
    
    @pytest.fixture
    def strategy_manager(self, mock_config, mock_market_api):
        if 'strategies' not in mock_config:
            mock_config['strategies'] = {}
        mock_config['strategies']['enabled'] = ['cross_sectional_momentum']
        
        # Add required trading config keys
        if 'trading' not in mock_config:
            mock_config['trading'] = {}
        mock_config['trading']['order_timeout_minutes'] = 5
        mock_config['trading']['enable_stale_order_cleanup'] = True
        mock_config['trading']['position_sync_interval'] = 60
        mock_config['trading']['enable_position_validation'] = True
        mock_config['trading']['position_monitoring_interval'] = 10
        mock_config['strategies']['ohlcv_limit'] = 100
        
        with patch('src.strategies.strategy_manager.StrategySelector'), \
             patch('src.strategies.strategy_manager.ExecutionEngine'), \
             patch('src.strategies.strategy_manager.DynamicPairSelector'), \
             patch('src.strategies.strategy_manager.PerformanceTracker'):
             
             manager = StrategyManager(mock_config, mock_market_api)
             return manager

    def test_is_data_ready_global_freshness_check(self, strategy_manager):
        """Test that _is_data_ready_for_symbol uses global freshness check."""
        symbol = "BTC"
        
        # Mock other checks to pass
        strategy_manager.market_api.get_ohlcv = MagicMock(return_value=[1]*20)
        mock_cache_data = {'5m': [1]*5, '15m': [1]*5, '1h': [1]*5, '4h': [1]*5}
        # Update: Mock nested cache structure (ohlcv_cache.cache.get)
        mock_cache_data = {'5m': [1]*5, '15m': [1]*5, '1h': [1]*5, '4h': [1]*5}
        
        # Create a mock for the cache attribute
        mock_cache_attr = MagicMock()
        mock_cache_attr.get.return_value = mock_cache_data
        
        # Assign it to strategy_manager.market_api.ohlcv_cache.cache
        strategy_manager.market_api.ohlcv_cache.cache = mock_cache_attr
        strategy_manager.market_api._subscribed_symbols = {symbol}
        
        # Mock health monitor
        strategy_manager.market_api.health_monitor = MagicMock()
        
        # Case 1: Global connection STALE -> Should return False
        strategy_manager.market_api.health_monitor.is_ws_data_fresh.return_value = False
        assert strategy_manager._is_data_ready_for_symbol(symbol) is False
        # Verify called WITHOUT arguments (global check)
        strategy_manager.market_api.health_monitor.is_ws_data_fresh.assert_called_with()
        
        # Case 2: Global connection FRESH -> Should return True
        strategy_manager.market_api.health_monitor.is_ws_data_fresh.return_value = True
        assert strategy_manager._is_data_ready_for_symbol(symbol) is True
